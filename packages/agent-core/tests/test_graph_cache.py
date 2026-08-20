"""SHA256 cache manifest + atomic graph.json store."""
from __future__ import annotations

import json
from pathlib import Path

from agent_core.knowledge.graph.cache import (
    CACHE_VERSION,
    GraphCache,
    GraphCacheState,
)
from agent_core.knowledge.graph.model import (
    EDGE_CALLS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
)
from agent_core.knowledge.graph.parser import file_sha256
from agent_core.knowledge.graph.store import (
    GRAPH_FILE,
    load_cached,
    load_repo_graph,
    save_repo_graph,
)


def _sample_graph() -> RepoGraph:
    return RepoGraph(
        repo=RepoMeta(name="demo", head_sha="abc123", langs=["python"], built_at=0),
        stats={"files": 1, "nodes": 2, "edges": 1},
        nodes=[
            GraphNode(id="f:main.py", type="file", name="main.py"),
            GraphNode(id="s:main.py:run", type="function", name="run", line=1),
        ],
        edges=[
            GraphEdge(src="f:main.py", dst="s:main.py:run", type=EDGE_CALLS),
        ],
    )


def test_sha256_stable(tmp_path: Path):
    path = tmp_path / "a.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    first = file_sha256(path)
    second = file_sha256(path)
    assert first == second
    path.write_text("def f():\n    return 2\n", encoding="utf-8")
    assert file_sha256(path) != first


def test_manifest_roundtrip(tmp_path: Path):
    cache = GraphCache(tmp_path / "cache")
    state = GraphCacheState()
    state.files["a.py"] = {"sha": "x" * 64, "lang": "python"}
    cache.save(state)
    loaded = cache.load()
    assert loaded.version == CACHE_VERSION
    assert loaded.files["a.py"]["sha"] == "x" * 64
    # atomic write leaves no temp litter
    assert list(cache.cache_dir.glob("*.tmp")) == []


def test_corrupt_manifest_rebuilds(tmp_path: Path):
    cache = GraphCache(tmp_path / "cache")
    cache.cache_dir.mkdir(parents=True)
    (cache.cache_dir / "index.json").write_text("{ not json", encoding="utf-8")
    state = cache.load()
    assert state.files == {}


def test_version_mismatch_rebuilds(tmp_path: Path):
    cache = GraphCache(tmp_path / "cache")
    cache.save(GraphCacheState(version=99, files={"a.py": {}}))
    assert cache.load().files == {}


def test_drop_and_set(tmp_path: Path):
    cache = GraphCache(tmp_path / "cache")
    state = GraphCacheState()
    cache.set_file(state, "a.py", _Result())
    cache.set_file(state, "b.py", _Result())
    cache.drop(state, "a.py")
    assert list(state.files) == ["b.py"]
    assert cache.matches(state, "b.py", "f" * 64)
    assert not cache.matches(state, "b.py", "0" * 64)


class _Result:
    sha256 = "f" * 64
    lang = "python"
    symbols = [{"name": "run", "type": "function", "line": 1}]
    edges = [{"type": EDGE_CALLS, "target": "run", "from": None}]
    imports = []
    stats = {"error": None}
    lines = 3


def test_graph_store_roundtrip(tmp_path: Path):
    kg_dir = tmp_path / "kg"
    save_repo_graph(_sample_graph(), kg_dir)
    assert (kg_dir / GRAPH_FILE).is_file()
    loaded = load_repo_graph(kg_dir)
    assert loaded is not None
    assert loaded.repo.name == "demo"
    assert loaded.node("s:main.py:run").line == 1
    assert loaded.edges[0].type == EDGE_CALLS
    assert list(kg_dir.glob("*.tmp")) == []


def test_corrupt_graph_returns_none(tmp_path: Path):
    kg_dir = tmp_path / "kg"
    kg_dir.mkdir()
    (kg_dir / GRAPH_FILE).write_text("{ broken", encoding="utf-8")
    assert load_repo_graph(kg_dir) is None
    # corrupt graph is not cached either
    assert load_cached(kg_dir) is None


def test_load_cached_invalidates_on_change(tmp_path: Path):
    kg_dir = tmp_path / "kg"
    save_repo_graph(_sample_graph(), kg_dir)
    first = load_cached(kg_dir)
    assert first is not None and first.stats["nodes"] == 2
    # same file -> memoized instance
    assert load_cached(kg_dir) is first

    second_graph = _sample_graph()
    second_graph.stats["nodes"] = 99
    save_repo_graph(second_graph, kg_dir)
    # mtime/size changed -> fresh read, not the stale memo
    fresh = load_cached(kg_dir)
    assert fresh is not None and fresh.stats["nodes"] == 99
    assert json.loads((kg_dir / GRAPH_FILE).read_text(encoding="utf-8"))["stats"]["nodes"] == 99
