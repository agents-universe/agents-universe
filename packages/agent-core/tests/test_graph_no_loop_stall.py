"""Event-loop liveness + output caps for the repo knowledge graph.

Regression for the production disconnect: build_repo_graph's sync tail
(manifest json.load/dump, graph assembly, report render) used to run on the
event loop — a large .NET checkout froze it for seconds, uvicorn's WS ping
went unanswered, and the server closed the socket mid-turn.

Covers, in one place:
- the build tail runs in worker threads (identity check + gap ticker),
- bin/obj/.vs never enter the graph (tracked and walk-fallback paths),
- explicit builds refuse trees over MAX_BUILD_FILES,
- git listing subprocesses are bounded by _GIT_TIMEOUT_S,
- builds serialize per kg_dir (auto vs explicit),
- repo_graph query loads run off-loop,
- neighbors/impact result lists are capped,
- tool_call_end payloads are clipped to the WS frame budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
import threading
import time
from pathlib import Path

import pytest

import agent_core.knowledge.graph.builder as builder
import agent_core.tools.repo_graph as repo_graph_mod
from agent_core.knowledge.graph.builder import build_repo_graph, maybe_build_auto
from agent_core.knowledge.graph.cache import GraphCache
from agent_core.knowledge.graph.languages import detect_language
from agent_core.knowledge.graph.model import (
    EDGE_CALLS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
    file_id,
    now_epoch,
    repo_graph_dir,
    symbol_id,
)
from agent_core.knowledge.graph.parser import FileParseResult
from agent_core.knowledge.graph.queries import (
    IMPACT_LIMIT,
    NEIGHBOR_LIMIT,
    impact_set,
    neighbors,
)
from agent_core.knowledge.graph.store import load_cached
from agent_core.tools.base import ToolContext
from agent_core.tools.repo_graph import RepoGraphTool

# Gap budget for the ticker assertion: the injected sleeps below are 0.4s, so
# a regression (tail back on the loop) trips this clearly; incidental
# scheduler noise on a loaded CI box stays well under it.
_MAX_LOOP_GAP_S = 0.2
_SLEEP_S = 0.4


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _seed(path: Path, files: dict[str, str], git: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if git:
        _run("init", "-b", "main", cwd=path)
        _run("config", "user.email", "t@t.t", cwd=path)
        _run("config", "user.name", "t", cwd=path)
        _run("add", ".", cwd=path)
        _run("commit", "-m", "seed", cwd=path)
    return path


def _fake_parse(path: Path, rel: str) -> FileParseResult:
    """Grammar-free parse: keeps these tests hermetic (no tree-sitter/network)."""
    data = path.read_bytes()
    return FileParseResult(
        sha256=hashlib.sha256(data).hexdigest(),
        lang=detect_language(rel) or "text",
        stats={"error": None},
        lines=data.count(b"\n") + 1,
    )


def _make_context(project: Path) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=str(project),
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )


async def _record_max_gap(work) -> float:
    """Run `work` (a coroutine) while a ticker records loop.time() gaps."""
    gaps: list[float] = []

    async def _ticker() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(_ticker())
    try:
        await work
    finally:
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
    assert gaps, "ticker never ran"
    return max(gaps)


# ---------------------------------------------------------------------------
# Headline: the build tail must not run on the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_tail_sleeps_happen_off_loop(tmp_path: Path, monkeypatch):
    """Injected 0.4s sleeps in _assemble_graph/cache.save must not gap the loop."""
    repo = _seed(tmp_path / "repo", {"main.py": "def main():\n    return 1\n"})
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    real_assemble = builder._assemble_graph

    def slow_assemble(*args, **kwargs):
        time.sleep(_SLEEP_S)
        return real_assemble(*args, **kwargs)

    real_save = GraphCache.save

    def slow_save(self, state):
        time.sleep(_SLEEP_S)
        return real_save(self, state)

    monkeypatch.setattr(builder, "_assemble_graph", slow_assemble)
    monkeypatch.setattr(GraphCache, "save", slow_save)

    async def _work() -> None:
        summary = await build_repo_graph(repo, tmp_path / "kg")
        assert summary["status"] == "built"

    max_gap = await _record_max_gap(_work())
    assert max_gap < _MAX_LOOP_GAP_S, f"event loop stalled {max_gap:.3f}s during build"


@pytest.mark.asyncio
async def test_sync_tail_runs_in_worker_threads(tmp_path: Path, monkeypatch):
    """Deterministic version: every heavy sync step records a non-main thread."""
    repo = _seed(tmp_path / "repo", {"main.py": "def main():\n    return 1\n"})
    # Force the walk fallback so _walk_files is exercised too.
    async def _no_git(_repo):
        return None

    monkeypatch.setattr(builder, "_tracked_files", _no_git)
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    main_ident = threading.get_ident()
    idents: dict[str, int] = {}

    def record(name, fn):
        def wrapper(*args, **kwargs):
            idents[name] = threading.get_ident()
            return fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(builder, "_walk_files", record("walk", builder._walk_files))
    monkeypatch.setattr(builder, "_candidate_files", record("candidates", builder._candidate_files))
    monkeypatch.setattr(builder, "load_repo_graph", record("load_graph", builder.load_repo_graph))
    monkeypatch.setattr(builder, "_assemble_graph", record("assemble", builder._assemble_graph))
    monkeypatch.setattr(builder, "render_report", record("report", builder.render_report))
    monkeypatch.setattr(builder, "save_repo_graph", record("save_graph", builder.save_repo_graph))
    monkeypatch.setattr(builder, "compact_map", record("compact_map", builder.compact_map))
    monkeypatch.setattr(GraphCache, "load", record("cache_load", GraphCache.load))
    monkeypatch.setattr(GraphCache, "save", record("cache_save", GraphCache.save))

    summary = await build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"
    expected = (
        "walk", "candidates", "load_graph", "assemble", "report",
        "save_graph", "compact_map", "cache_load", "cache_save",
    )
    for name in expected:
        assert name in idents, f"{name} never ran"
        assert idents[name] != main_ident, f"{name} ran on the event loop"


@pytest.mark.asyncio
async def test_large_dotnet_tree_build_keeps_loop_live(tmp_path: Path, monkeypatch):
    """Real tree: src .cs files plus a bin/obj flood — excluded, and no stall."""
    files: dict[str, str] = {}
    for i in range(20):
        for j in range(30):  # 600 source files
            files[f"src/Ns{i}/Cls{j}.cs"] = f"namespace Ns{i} {{ public class Cls{j} {{}} }}\n"
    for i in range(60):  # 1200 generated files under build output
        for j in range(20):
            files[f"bin/Debug/net8/Gen{i}_{j}.cs"] = f"// generated {i}_{j}\n"
            files[f"obj/Debug/Obj{i}_{j}.cs"] = f"// generated obj {i}_{j}\n"
    repo = _seed(tmp_path / "dotnet", files)
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    async def _work() -> None:
        summary = await build_repo_graph(repo, tmp_path / "kg")
        assert summary["status"] == "built"
        assert summary["stats"]["files"] == 600  # bin/obj never indexed

    max_gap = await _record_max_gap(_work())
    assert max_gap < _MAX_LOOP_GAP_S, f"event loop stalled {max_gap:.3f}s"

    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    offenders = [n.name for n in graph.nodes if n.name.startswith(("bin/", "obj/"))]
    assert not offenders, f"build output leaked into graph: {offenders[:5]}"


# ---------------------------------------------------------------------------
# bin/obj/.vs exclusion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bin_obj_excluded_when_git_tracked(tmp_path: Path, monkeypatch):
    repo = _seed(tmp_path / "tracked", {
        "src/Main.cs": "namespace App { public class Main {} }\n",
        "bin/Generated.cs": "namespace App { public class Gen {} }\n",
        "obj/Obj.cs": "namespace App { public class Obj {} }\n",
        ".vs/Editor.cs": "class Vs {} \n",
    })
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    summary = await build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"
    assert summary["stats"]["files"] == 1
    graph = load_cached(tmp_path / "kg")
    names = {n.name for n in graph.nodes if n.type == "file"}
    assert names == {"src/Main.cs"}


@pytest.mark.asyncio
async def test_bin_obj_excluded_in_walk_fallback(tmp_path: Path, monkeypatch):
    repo = _seed(tmp_path / "walked", {
        "src/Main.cs": "namespace App { public class Main {} }\n",
        "bin/Generated.cs": "namespace App { public class Gen {} }\n",
        "obj/Obj.cs": "namespace App { public class Obj {} }\n",
    }, git=False)

    async def _no_git(_repo):
        return None

    monkeypatch.setattr(builder, "_tracked_files", _no_git)
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    summary = await build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"
    assert summary["stats"]["files"] == 1


# ---------------------------------------------------------------------------
# Explicit build cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_build_refuses_oversized_tree(tmp_path: Path, monkeypatch):
    proj = _seed(tmp_path / "proj" / "repos" / "big", {
        "a.py": "def a():\n    return 1\n",
        "b.py": "def b():\n    return 2\n",
    })
    monkeypatch.setattr(builder, "MAX_BUILD_FILES", 1)
    result = await RepoGraphTool().execute({"operation": "build"}, _make_context(tmp_path / "proj"))
    assert result["status"] == "skipped"
    assert result["reason"] == "too_many_files"
    assert result["files"] == 2 and result["max"] == 1
    assert result["hint"] and "search/read" in result["hint"]


# ---------------------------------------------------------------------------
# Git subprocess timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_listing_times_out_and_kills(monkeypatch):
    monkeypatch.setattr(builder, "_GIT_TIMEOUT_S", 0.05)
    spawned: list = []

    class _HungProc:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.sleep(5)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_exec(*args, **kwargs):
        proc = _HungProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(builder.asyncio, "create_subprocess_exec", _fake_exec)
    result = await builder._tracked_files(Path("."))
    assert result is None
    assert spawned and spawned[0].killed, "hung git process was not killed"


# ---------------------------------------------------------------------------
# Build lock: auto and explicit builds serialize per kg_dir
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_builds_serialize_per_kg_dir(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    repo = proj / "repos" / "locked"
    _seed(repo, {"main.py": "def main():\n    return 1\n"})
    monkeypatch.setattr(builder, "parse_file", _fake_parse)

    entered = threading.Event()
    release = threading.Event()
    real_assemble = builder._assemble_graph

    def blocking_assemble(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test never released the build"
        return real_assemble(*args, **kwargs)

    monkeypatch.setattr(builder, "_assemble_graph", blocking_assemble)

    kg_dir = repo_graph_dir(str(proj), "locked")
    explicit = asyncio.create_task(
        RepoGraphTool().execute({"operation": "build"}, _make_context(proj))
    )
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "explicit build never reached assembly"
    assert builder._BUILD_LOCKS[str(kg_dir)].locked(), "explicit build holds no lock"

    auto = asyncio.create_task(maybe_build_auto(repo, str(proj), "locked"))
    await asyncio.sleep(0.15)
    assert not auto.done(), "auto build ran while the explicit build held the lock"

    release.set()
    first = await explicit
    second = await auto
    assert first["status"] == "built"
    # force=False: the head is unchanged after the first build -> fast path.
    assert second["status"] == "up_to_date"


# ---------------------------------------------------------------------------
# Query path off-loop + result caps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_load_cached_off_loop(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    _seed(proj / "repos" / "sample", {"main.py": "def main():\n    return 1\n"})
    monkeypatch.setattr(builder, "parse_file", _fake_parse)
    await RepoGraphTool().execute({"operation": "build"}, _make_context(proj))

    main_ident = threading.get_ident()
    idents: list[int] = []
    real_load = repo_graph_mod.load_cached

    def wrapped(*args, **kwargs):
        idents.append(threading.get_ident())
        return real_load(*args, **kwargs)

    monkeypatch.setattr(repo_graph_mod, "load_cached", wrapped)
    result = await RepoGraphTool().execute(
        {"operation": "query", "query": "main"}, _make_context(proj))
    assert "matches" in result
    assert idents, "load_cached never ran"
    assert idents[0] != main_ident, "query path loaded graph.json on the event loop"


def _hub_graph(hub_degree: int = 300) -> RepoGraph:
    hub = symbol_id("src/Hub.cs", "Hub.Calc")
    nodes = [GraphNode(id=hub, type="method", name="Hub.Calc")]
    edges = []
    for i in range(hub_degree):
        fid = file_id(f"src/F{i}.cs")
        nodes.append(GraphNode(id=fid, type="file", name=f"src/F{i}.cs", lang="csharp"))
        edges.append(GraphEdge(src=fid, dst=hub, type=EDGE_CALLS))
    return RepoGraph(
        repo=RepoMeta(name="hub", head_sha="", langs=["csharp"], built_at=now_epoch()),
        nodes=nodes,
        edges=edges,
    )


def test_neighbors_result_list_is_capped():
    graph = _hub_graph(300)
    result = neighbors(graph, symbol_id("src/Hub.cs", "Hub.Calc"), depth=1)
    assert result["count"] == 300  # exact despite the cap
    assert len(result["neighbors"]) == NEIGHBOR_LIMIT
    assert result["truncated"] is True


def test_impact_result_lists_are_capped():
    graph = _hub_graph(300)
    result = impact_set(graph, symbol_id("src/Hub.cs", "Hub.Calc"))
    assert result["count"] == 300
    assert len(result["affected_nodes"]) == IMPACT_LIMIT
    assert len(result["affected_files"]) == IMPACT_LIMIT
    assert result["truncated"] is True


def test_uncapped_small_results_have_no_truncated_flag():
    graph = _hub_graph(3)
    small = neighbors(graph, symbol_id("src/Hub.cs", "Hub.Calc"), depth=1)
    assert small["count"] == 3 and "truncated" not in small
    small_impact = impact_set(graph, symbol_id("src/Hub.cs", "Hub.Calc"))
    assert small_impact["count"] == 3 and "truncated" not in small_impact


# ---------------------------------------------------------------------------
# tool_call_end output clipping
# ---------------------------------------------------------------------------

def test_clip_tool_output_bounds_ws_frames():
    from agent_core.agent import _MAX_TOOL_OUTPUT_CHARS, _clip_tool_output

    small = {"ok": True}
    assert _clip_tool_output(small) is small  # under cap -> identity
    assert _clip_tool_output("not a dict") == "not a dict"

    big = {"data": "x" * (_MAX_TOOL_OUTPUT_CHARS + 10)}
    clipped = _clip_tool_output(big)
    assert clipped["truncated"] is True
    assert clipped["bytes"] > _MAX_TOOL_OUTPUT_CHARS
    assert len(clipped["preview"]) <= 50_000
