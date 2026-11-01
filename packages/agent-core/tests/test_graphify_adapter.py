"""graphify_adapter: the sole graphify choke-point.

Covers the upstream constraints the adapter exists to enforce (module
docstring of graphify_adapter): the process-wide lock, the lazy import
with timing, incremental SHA256 cache reuse, failure degradation back to
the builder, and the schema-v2 conversion rules (edge direction from
_src/_tgt, qname derivation through containment, collision-safe ids).
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import agent_core.knowledge.graph.graphify_adapter as adapter
from agent_core.knowledge.graph.builder import build_repo_graph
from agent_core.knowledge.graph.graphify_adapter import (
    GraphifyResult,
    GraphifyUnavailable,
    _ensure_imported,
    _GRAPHIFY_LOCK,
    build_graph,
    lang_of,
    to_repo_graph,
)
from agent_core.knowledge.graph.model import (
    EDGE_CALLS,
    EDGE_FOREIGN_KEY,
    EDGE_IMPORTS,
    NODE_CLASS,
    NODE_FILE,
    NODE_FUNCTION,
    NODE_TABLE,
)

@pytest.fixture(autouse=True)
def graphify_available():
    try:
        _ensure_imported()
    except GraphifyUnavailable:
        pytest.skip("graphifyy not installed")


def _run(*args: str, cwd: Path) -> None:
    import subprocess
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _seed(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _run("init", "-b", "main", cwd=path)
    _run("config", "user.email", "t@t.t", cwd=path)
    _run("config", "user.name", "t", cwd=path)
    _run("add", ".", cwd=path)
    _run("commit", "-m", "seed", cwd=path)
    return path


# ---------------------------------------------------------------------------
# Lock + lazy import
# ---------------------------------------------------------------------------

def test_lock_is_process_wide_reentrant():
    assert isinstance(_GRAPHIFY_LOCK, type(threading.RLock()))
    # RLock: the same thread can nest (extract -> build both take it).
    assert _GRAPHIFY_LOCK.acquire(blocking=False)
    try:
        assert _GRAPHIFY_LOCK.acquire(blocking=False)
        _GRAPHIFY_LOCK.release()
    finally:
        _GRAPHIFY_LOCK.release()


def test_lazy_import_caches_symbols_and_timing():
    with _GRAPHIFY_LOCK:
        first = _ensure_imported()
        second = _ensure_imported()
    assert first is second  # cached, not re-imported
    for key in ("extract", "build", "cluster", "god_nodes",
                "surprising_connections", "score_all",
                "label_communities_by_hub"):
        assert key in first and callable(first[key])
    assert adapter._IMPORT_MS >= 0


def test_graphify_unavailable_message_is_actionable(monkeypatch):
    """Import failure surfaces install instructions, not a bare ImportError."""
    monkeypatch.setattr(adapter, "_IMPORTS", {})

    def _boom():
        raise ImportError("no graphify")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("graphify"):
            raise ImportError("no graphify")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(GraphifyUnavailable, match="graphifyy\\[sql\\]==0.9.100"):
        _ensure_imported()


# ---------------------------------------------------------------------------
# Incremental extraction (graphify's own SHA256 cache)
# ---------------------------------------------------------------------------

def test_second_build_reuses_unchanged_files(tmp_path: Path):
    repo = _seed(tmp_path / "repo", {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def beta():\n    return 2\n",
        "c.py": "def gamma():\n    return 3\n",
    })
    cache = tmp_path / "cache"
    paths = [repo / "a.py", repo / "b.py", repo / "c.py"]

    first = build_graph(paths, root=repo, cache_root=cache)
    assert first.extracted == 3
    assert first.failed_sources == []

    second = build_graph(paths, root=repo, cache_root=cache)
    assert second.extracted == 0  # all served from the SHA256 cache
    assert second.graph.number_of_nodes() > 0


def test_cache_root_isolated_from_repo_dir(tmp_path: Path):
    """The graphify cache never lands inside the checkout being indexed."""
    repo = _seed(tmp_path / "repo", {"a.py": "def alpha():\n    return 1\n"})
    cache = tmp_path / "elsewhere" / "graphify-out"
    build_graph([repo / "a.py"], root=repo, cache_root=cache)
    assert cache.is_dir()
    assert not (repo / "graphify-out").exists()
    assert not (repo / ".graphify").exists()


# ---------------------------------------------------------------------------
# Concurrency: two roots at once must not cross-contaminate
# ---------------------------------------------------------------------------

def test_concurrent_builds_from_two_roots_stay_isolated(tmp_path: Path):
    """Two threads, two repos, barrier-synchronized — the lock must keep
    graphify's _ACTIVE_SCAN_ROOT from bleeding one root into the other."""
    repo_a = _seed(tmp_path / "alpha", {"a.py": "def unique_alpha():\n    return 1\n"})
    repo_b = _seed(tmp_path / "beta", {"b.py": "def unique_beta():\n    return 2\n"})
    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, GraphifyResult] = {}
    errors: list[BaseException] = []

    def worker(name: str, root: Path) -> None:
        try:
            barrier.wait()
            results[name] = build_graph(
                [root / ("a.py" if name == "alpha" else "b.py")],
                root=root, cache_root=tmp_path / f"cache-{name}",
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("alpha", repo_a)),
        threading.Thread(target=worker, args=("beta", repo_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert set(results) == {"alpha", "beta"}
    labels_a = {str(d.get("label") or "").rstrip("()")
                for _, d in results["alpha"].graph.nodes(data=True)}
    labels_b = {str(d.get("label") or "").rstrip("()")
                for _, d in results["beta"].graph.nodes(data=True)}
    assert "unique_alpha" in labels_a and "unique_beta" not in labels_a
    assert "unique_beta" in labels_b and "unique_alpha" not in labels_b


# ---------------------------------------------------------------------------
# Failure degradation: builder must return failed, never raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_builder_degrades_to_failed_when_graphify_missing(
    tmp_path: Path, monkeypatch
):
    repo = _seed(tmp_path / "repo", {"a.py": "def alpha():\n    return 1\n"})

    def _unavailable(*args, **kwargs):
        raise GraphifyUnavailable("graphifyy is not importable (test)")

    monkeypatch.setattr("agent_core.knowledge.graph.builder.build_graph", _unavailable)
    summary = await build_repo_graph(repo, tmp_path / "kg")
    assert summary["status"] == "failed"
    assert "graphifyy" in summary["reason"]


@pytest.mark.asyncio
async def test_builder_unexpected_exception_becomes_error_via_tool(
    tmp_path: Path, monkeypatch
):
    """The tool layer catches everything — a graph failure is an error dict,
    never a raised exception that kills the turn."""
    import subprocess
    from agent_core.tools.base import ToolContext
    from agent_core.tools.repo_graph import RepoGraphTool

    repo = tmp_path / "proj" / "repos" / "boom"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t.t"],
                 ["config", "user.name", "t"], ["add", "."],
                 ["commit", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True)

    def _explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agent_core.knowledge.graph.builder.build_graph", _explode)
    ctx = ToolContext(
        project_id="proj", project_fs_path=str(tmp_path / "proj"),
        conversation_id="c", user_id="u", db_session=None,
    )
    result = await RepoGraphTool().execute({"operation": "build"}, ctx)
    assert "error" in result and "kaboom" in result["error"]


# ---------------------------------------------------------------------------
# Conversion: to_repo_graph unit tests on a synthetic nx.Graph
# ---------------------------------------------------------------------------

def _synth_graph():
    """Minimal nx.Graph shaped like graphify's output (undirected, arc
    markers in _src/_tgt, relation labels, containment edges)."""
    import networkx as nx

    g = nx.Graph()
    # file node: label == basename(source_file), no type attr
    g.add_node("n_file", label="schema.sql", source_file="db/schema.sql")
    g.add_node("n_orders", label="orders", source_file="db/schema.sql",
               source_location="L3")
    g.add_node("n_users", label="users", source_file="db/schema.sql",
               source_location="L1")
    # FK: orders.user_id REFERENCES users(id) — direction orders -> users
    g.add_edge("n_orders", "n_users", relation="foreign_key",
               _src="n_orders", _tgt="n_users")

    g.add_node("n_pyfile", label="svc.py", source_file="svc.py")
    g.add_node("n_svc", label="Service", source_file="svc.py",
               _callable_class=True, source_location="L10")
    g.add_edge("n_pyfile", "n_svc", relation="contains",
               _src="n_pyfile", _tgt="n_svc")
    g.add_node("n_run", label="run()", source_file="svc.py",
               _callable=True, source_location="L11")
    g.add_edge("n_svc", "n_run", relation="method",
               _src="n_svc", _tgt="n_run")
    g.add_edge("n_run", "n_users", relation="calls",
               _src="n_run", _tgt="n_users")
    return g


def test_conversion_maps_types_qnames_and_fk_direction():
    result = GraphifyResult(
        graph=_synth_graph(),
        extracted=2,
        failed_sources=[],
        import_ms=123,
    )
    graph = to_repo_graph(
        result, repo_name="demo", head_sha="abc",
        candidates=["db/schema.sql", "svc.py"],
        langs=["python", "sql"],
        extra_stats={"skipped_files": {}},
    )

    by_id = {n.id: n for n in graph.nodes}
    # file nodes keyed by rel
    assert by_id["f:db/schema.sql"].type == NODE_FILE
    assert by_id["f:svc.py"].type == NODE_FILE
    # SQL symbols are tables
    assert by_id["s:db/schema.sql:orders"].type == NODE_TABLE
    assert by_id["s:db/schema.sql:users"].type == NODE_TABLE
    # class/function typing from graphify attrs
    assert by_id["s:svc.py:Service"].type == NODE_CLASS
    assert by_id["s:svc.py:Service.run"].type == NODE_FUNCTION

    edges = {(e.src, e.dst, e.type) for e in graph.edges}
    # direction comes from _src/_tgt, not arbitrary undirected order
    assert ("s:db/schema.sql:orders", "s:db/schema.sql:users",
            EDGE_FOREIGN_KEY) in edges
    # contains/method edges are dropped (containment is id-derived)
    assert not any(t == "contains" or t == "method" for _, _, t in edges)
    # no file->class edge stands in for the dropped contains
    assert ("f:svc.py", "s:svc.py:Service", EDGE_IMPORTS) not in edges
    # calls edge survives with correct direction
    assert ("s:svc.py:Service.run", "s:db/schema.sql:users",
            EDGE_CALLS) in edges

    stats = graph.stats
    assert stats["files"] == 2
    assert stats["parsed"] == 2
    assert stats["reused"] == 0
    assert stats["failed"] == 0
    assert stats["lang_coverage"]["python"] == {"files": 1, "with_symbols": 1}
    assert stats["lang_coverage"]["sql"] == {"files": 1, "with_symbols": 1}
    assert graph.repo.head_sha == "abc"
    assert graph.repo.name == "demo"


def test_conversion_dedupes_and_never_merges_collisions():
    """Two nodes with the same rel+qname must both survive, salted — never
    silently merged into one."""
    import networkx as nx

    g = nx.Graph()
    for i, node_id in enumerate(("dup1", "dup2")):
        g.add_node(node_id, label="helper", source_file="m.py",
                   _callable=True, source_location=f"L{i + 1}")
    result = GraphifyResult(graph=g, extracted=1)
    graph = to_repo_graph(
        result, repo_name="r", head_sha="",
        candidates=["m.py"], langs=["python"],
    )
    helpers = [n for n in graph.nodes if n.name == "helper"]
    assert len(helpers) == 2, "collision was silently merged"
    assert helpers[0].id != helpers[1].id
    assert all(n.id.startswith("s:m.py:helper") for n in helpers)


def test_conversion_counts_unresolved_edges():
    """An edge whose endpoint was dropped (no source_file) counts as
    unresolved instead of crashing or vanishing silently."""
    import networkx as nx

    g = nx.Graph()
    g.add_node("keep", label="a.py", source_file="a.py")
    g.add_node("ghost", label="mystery")  # no source_file -> dropped
    g.add_edge("keep", "ghost", relation="imports", _src="keep", _tgt="ghost")
    result = GraphifyResult(graph=g, extracted=1)
    graph = to_repo_graph(
        result, repo_name="r", head_sha="",
        candidates=["a.py"], langs=["python"],
    )
    assert graph.stats["unresolved_calls"] == 1
    assert all(e.src != "f:a.py" for e in graph.edges) or not graph.edges


def test_lang_of_covers_contract_extensions():
    assert lang_of("src/main.py") == "python"
    assert lang_of("a/b/c.vue") == "vue"
    assert lang_of("X.CS") == "csharp"  # case-insensitive
    assert lang_of("schema.SQL") == "sql"
    assert lang_of("notes.txt") is None


def test_sql_same_file_references_becomes_foreign_key():
    """SQL table->table `references` inside one file is a FK, not a generic
    references edge."""
    import networkx as nx

    g = nx.Graph()
    g.add_node("f", label="s.sql", source_file="s.sql")
    g.add_node("t1", label="parent", source_file="s.sql")
    g.add_node("t2", label="child", source_file="s.sql")
    g.add_edge("t1", "t2", relation="references", _src="t2", _tgt="t1")
    result = GraphifyResult(graph=g, extracted=1)
    graph = to_repo_graph(
        result, repo_name="r", head_sha="",
        candidates=["s.sql"], langs=["sql"],
    )
    edges = {(e.src, e.dst, e.type) for e in graph.edges}
    assert ("s:s.sql:child", "s:s.sql:parent", EDGE_FOREIGN_KEY) in edges
