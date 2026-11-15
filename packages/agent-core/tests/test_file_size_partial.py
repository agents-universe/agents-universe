"""Oversized files become partial file nodes; size caps are per-language.

Pins the split that used to be a silent whole-file skip:
- size > cap  → bare file node with partial=True (accepted in stats/manifest,
  never sent to graphify), reported under "Partial files" in graph_report.md,
- missing/unreadable → hard skip (skipped_files, no node) — unchanged,
- SQL keeps its own cap constant (AST walk is far costlier than code),
- graph.json carries the current schema version (partial nodes are why).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import agent_core.knowledge.graph.builder as builder
from agent_core.knowledge.graph.graphify_adapter import GraphifyResult, to_repo_graph
from agent_core.knowledge.graph.model import (
    NODE_FILE,
    SCHEMA_VERSION,
    file_id,
)
from agent_core.knowledge.graph.store import load_cached


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _seed(path: Path, files: dict[str, str | bytes]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=path)
    _run("config", "user.email", "t@t.t", cwd=path)
    _run("config", "user.name", "t", cwd=path)
    _run("add", ".", cwd=path)
    _run("commit", "-m", "seed", cwd=path)
    return path


@pytest.fixture
def graphify_available():
    try:
        import graphify.extract  # noqa: F401
    except ImportError:
        pytest.skip("graphifyy not installed")


# ---------------------------------------------------------------------------
# Adapter: partial_files → bare file nodes
# ---------------------------------------------------------------------------

def test_partial_files_injected_as_bare_file_nodes():
    """A candidate the extractor never saw still gets a file node, marked
    partial, with no symbols and no slots in with_symbols/lang coverage."""
    import networkx as nx

    g = nx.Graph()
    g.add_node("f1", label="svc.py", source_file="svc.py")
    g.add_node("s1", label="Service", source_file="svc.py",
               _callable_class=True, source_location="L1")
    g.add_edge("f1", "s1", relation="contains", _src="f1", _tgt="s1")

    graph = to_repo_graph(
        GraphifyResult(graph=g, extracted=1), repo_name="r", head_sha="",
        candidates=["svc.py", "big.py"], langs=["python"],
        partial_files=["big.py"],
        extra_stats={"partial_files": {"big.py": "over 10MB limit; file node only"},
                     "partial_files_count": 1},
    )
    by_id = {n.id: n for n in graph.nodes}
    big = by_id[file_id("big.py")]
    assert big.type == NODE_FILE
    assert big.partial is True
    assert big.name == "big.py"
    # no symbols leaked out of the partial file
    assert not any(n.id.startswith("s:big.py:") for n in graph.nodes)
    assert not any(e.src == file_id("big.py") or e.dst == file_id("big.py")
                   for e in graph.edges)

    stats = graph.stats
    assert stats["files"] == 2
    assert stats["with_symbols"] == 1  # svc.py only
    assert stats["lang_coverage"]["python"] == {"files": 2, "with_symbols": 1}
    assert stats["partial_files_count"] == 1


def test_partial_flag_skipped_when_graphify_already_emitted_the_file():
    """Defensive: a file present in the extract keeps its node — no dupes."""
    import networkx as nx

    g = nx.Graph()
    g.add_node("f1", label="big.py", source_file="big.py")
    graph = to_repo_graph(
        GraphifyResult(graph=g, extracted=1), repo_name="r", head_sha="",
        candidates=["big.py"], langs=["python"],
        partial_files=["big.py"],
    )
    assert sum(1 for n in graph.nodes if n.id == file_id("big.py")) == 1


def test_partial_roundtrips_through_json_codec():
    from agent_core.knowledge.graph.model import GraphNode

    node = GraphNode(id="f:big.py", type=NODE_FILE, name="big.py", partial=True)
    data = node.to_dict()
    assert data["partial"] is True
    back = GraphNode.from_dict(data)
    assert back.partial is True
    # lean nodes stay lean: no partial key for regular files
    plain = GraphNode(id="f:a.py", type=NODE_FILE, name="a.py")
    assert "partial" not in plain.to_dict()
    assert GraphNode.from_dict(plain.to_dict()).partial is False


# ---------------------------------------------------------------------------
# Builder integration: _prepare split + wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oversized_file_becomes_partial_node(tmp_path: Path, monkeypatch,
                                                   graphify_available):
    repo = _seed(tmp_path / "repo", {
        "small.py": "def small():\n    return 1\n",
        "big.py": "def big():\n    return 1\n" + "# pad\n" * 40,  # > 64 bytes
    })
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 64)

    summary = await builder.build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"

    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    by_id = {n.id: n for n in graph.nodes}
    big = by_id[file_id("big.py")]
    assert big.partial is True
    assert not any(n.id.startswith("s:big.py:") for n in graph.nodes)
    # the small file still extracts fully
    assert any(n.id.startswith("s:small.py:") for n in graph.nodes)

    stats = summary["stats"]
    assert stats["files"] == 2
    assert stats["partial_files_count"] == 1
    assert "big.py" in stats["partial_files"]
    assert "big.py" not in stats.get("skipped_files", {})
    assert "oversized" in summary["warning"] and "file nodes only" in summary["warning"]

    report = (tmp_path / "kg" / "graph_report.md").read_text(encoding="utf-8")
    assert "Partial files (oversized, file node only: 1)" in report
    assert "`big.py`" in report


@pytest.mark.asyncio
async def test_sql_uses_its_own_size_cap(tmp_path: Path, monkeypatch,
                                         graphify_available):
    """SQL is gated by SQL_MAX_FILE_BYTES alone — a tiny general cap must
    not push .sql files into partial, and a tiny SQL cap must not touch .py."""
    repo = _seed(tmp_path / "repo", {
        "app.py": "def app():\n    return 1\n" + "# pad\n" * 40,
        "schema.sql": "CREATE TABLE t (\n    id INTEGER\n);\n" + "-- pad\n" * 20,
    })
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 64)
    monkeypatch.setattr(builder, "SQL_MAX_FILE_BYTES", 64)

    summary = await builder.build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"
    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    by_id = {n.id: n for n in graph.nodes}
    # both oversized under their own caps → both partial
    assert by_id[file_id("schema.sql")].partial is True
    assert by_id[file_id("app.py")].partial is True
    assert summary["stats"]["partial_files_count"] == 2

    # now give SQL room: schema.sql extracts, app.py stays partial
    monkeypatch.setattr(builder, "SQL_MAX_FILE_BYTES", 10 * 1024 * 1024)
    from agent_core.knowledge.graph.store import invalidate_cached
    invalidate_cached(tmp_path / "kg")
    summary2 = await builder.build_repo_graph(repo, tmp_path / "kg", force=True)
    assert summary2["status"] == "built"
    graph2 = load_cached(tmp_path / "kg")
    assert graph2 is not None
    by_id2 = {n.id: n for n in graph2.nodes}
    assert by_id2[file_id("schema.sql")].partial is False
    assert any(n.id.startswith("s:schema.sql:") for n in graph2.nodes)
    assert by_id2[file_id("app.py")].partial is True


@pytest.mark.asyncio
async def test_missing_file_still_hard_skipped(tmp_path: Path, monkeypatch,
                                               graphify_available):
    """Deleted-but-tracked candidates stay in skipped_files with no node —
    the oversize branch must not swallow them."""
    repo = _seed(tmp_path / "repo", {
        "keep.py": "def keep():\n    return 1\n",
        "gone.py": "def gone():\n    return 2\n",
    })
    (repo / "gone.py").unlink()  # tracked in index, absent on disk

    summary = await builder.build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"
    graph = load_cached(tmp_path / "kg")
    assert graph is not None
    assert file_id("gone.py") not in {n.id for n in graph.nodes}
    assert "gone.py" in summary["stats"]["skipped_files"]
    assert summary["stats"].get("partial_files_count", 0) == 0


@pytest.mark.asyncio
async def test_graph_json_is_schema_v4_with_partial_only_on_partial_nodes(
    tmp_path: Path, monkeypatch, graphify_available
):
    repo = _seed(tmp_path / "repo", {
        "ok.py": "def ok():\n    return 1\n",
        "huge.py": "def huge():\n    return 1\n" + "# pad\n" * 40,
    })
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 64)
    summary = await builder.build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "built"

    data = json.loads((tmp_path / "kg" / "graph.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION == 4
    partial_ids = {n["id"] for n in data["nodes"] if n.get("partial")}
    assert partial_ids == {file_id("huge.py")}
    for node in data["nodes"]:
        if node["id"] != file_id("huge.py"):
            assert "partial" not in node


# ---------------------------------------------------------------------------
# Tier constants (defaults only — boundaries are monkeypatched elsewhere)
# ---------------------------------------------------------------------------

def test_default_limits_match_the_50k_10mb_contract():
    assert builder.AUTO_BUILD_MAX_FILES == 3000
    assert builder.MAX_BUILD_FILES == 50_000
    assert builder.MAX_FILE_BYTES == 10 * 1024 * 1024
    assert builder.SQL_MAX_FILE_BYTES == 10 * 1024 * 1024
