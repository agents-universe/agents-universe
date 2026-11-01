"""aggregate_project_graph: multi-repo id namespacing, overview rollup, caps.

The stored graph.json files keep repo-relative ids; only the aggregated
output namespaces them (``{repo}:{raw_id}`` / ``d:{slug}``). SQL object ids
share the ``s:`` prefix with symbols — classification is type-based so
tables survive the overview while functions roll up to their files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.knowledge.graph import project
from agent_core.knowledge.graph.model import (
    EDGE_CALLS,
    EDGE_FOREIGN_KEY,
    EDGE_IMPORTS,
    EDGE_READS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
    repo_graph_dir,
)


def _write_graph(fs: Path, repo_name: str, graph: RepoGraph) -> None:
    kg_dir = repo_graph_dir(str(fs), repo_name)
    kg_dir.mkdir(parents=True, exist_ok=True)
    (kg_dir / "graph.json").write_text(
        json.dumps(graph.to_dict()), encoding="utf-8"
    )


def _clone(fs: Path, repo_name: str) -> None:
    (fs / "repos" / repo_name / ".git").mkdir(parents=True, exist_ok=True)


def _api_graph() -> RepoGraph:
    """src/x.py imports src/y.py; both define callables that call each other."""
    return RepoGraph(
        repo=RepoMeta(name="api", head_sha="abc", langs=["python"], built_at=1.0),
        stats={"files": 2, "nodes": 5, "edges": 4},
        nodes=[
            GraphNode(id="f:src/x.py", type="file", name="src/x.py", lang="python", lines=10),
            GraphNode(id="f:src/y.py", type="file", name="src/y.py", lang="python", lines=20),
            GraphNode(id="s:src/x.py:foo", type="function", name="foo", line=3),
            GraphNode(id="s:src/y.py:bar", type="function", name="bar", line=7),
        ],
        edges=[
            GraphEdge(src="f:src/x.py", dst="f:src/y.py", type=EDGE_IMPORTS),
            GraphEdge(src="s:src/x.py:foo", dst="s:src/y.py:bar", type=EDGE_CALLS),
            GraphEdge(src="s:src/x.py:foo", dst="f:src/x.py", type=EDGE_CALLS),
        ],
    )


def _web_graph() -> RepoGraph:
    """Same f:src/x.py id as the api repo — the collision under test."""
    return RepoGraph(
        repo=RepoMeta(name="web", head_sha="def", langs=["typescript"], built_at=2.0),
        stats={"files": 1, "nodes": 1, "edges": 0},
        nodes=[GraphNode(id="f:src/x.py", type="file", name="src/x.py", lang="typescript")],
        edges=[],
    )


def _sql_graph() -> RepoGraph:
    """Orders --fk--> Customers plus a proc that reads Orders.

    Table ids share the ``s:`` prefix with symbol ids: the overview must keep
    them as tables, not roll them into their .sql file.
    """
    return RepoGraph(
        repo=RepoMeta(name="db", head_sha="ghi", langs=["sql"], built_at=3.0),
        stats={"files": 2, "nodes": 4, "edges": 2},
        nodes=[
            GraphNode(id="f:db/schema.sql", type="file", name="db/schema.sql", lang="sql"),
            GraphNode(id="s:db/schema.sql:dbo.Orders", type="table", name="dbo.Orders"),
            GraphNode(id="s:db/schema.sql:dbo.Orders.OrderId", type="column",
                      name="dbo.Orders.OrderId"),
            GraphNode(id="s:db/schema.sql:dbo.Customers", type="table", name="dbo.Customers"),
            GraphNode(id="s:db/procs/GetOrder.sql:dbo.GetOrder", type="procedure",
                      name="dbo.GetOrder"),
            GraphNode(id="f:db/procs/GetOrder.sql", type="file", name="db/procs/GetOrder.sql",
                      lang="sql"),
        ],
        edges=[
            GraphEdge(src="s:db/schema.sql:dbo.Orders",
                      dst="s:db/schema.sql:dbo.Customers", type=EDGE_FOREIGN_KEY),
            GraphEdge(src="s:db/procs/GetOrder.sql:dbo.GetOrder",
                      dst="s:db/schema.sql:dbo.Orders", type=EDGE_READS),
        ],
    )


DOCS = [
    {"slug": "domain/context", "title": "项目背景", "summary": "背景", "category": "domain",
     "cross_references": ["technical/api", "missing/slug"]},
    {"slug": "technical/api", "title": "API", "summary": "", "category": "technical",
     "cross_references": ["domain/context"]},
]


def _fs(tmp_path: Path) -> Path:
    _clone(tmp_path, "api")
    _clone(tmp_path, "web")
    _write_graph(tmp_path, "api", _api_graph())
    _write_graph(tmp_path, "web", _web_graph())
    return tmp_path


def test_colliding_ids_are_namespaced_per_repo(tmp_path: Path):
    payload = project.aggregate_project_graph(str(_fs(tmp_path)), docs=[])
    ids = [node["id"] for node in payload["nodes"]]
    assert "api:f:src/x.py" in ids
    assert "web:f:src/x.py" in ids
    assert len(ids) == len(set(ids))
    by_id = {node["id"]: node for node in payload["nodes"]}
    assert by_id["api:f:src/x.py"]["repo"] == "api"
    assert by_id["web:f:src/x.py"]["lang"] == "typescript"
    assert payload["counts"]["repos"] == 2
    assert payload["needs_build"] is False


def test_overview_drops_symbols_and_rolls_edges_up(tmp_path: Path):
    payload = project.aggregate_project_graph(str(_fs(tmp_path)), docs=[], level="overview")
    ids = {node["id"] for node in payload["nodes"]}
    assert "api:s:src/x.py:foo" not in ids
    # foo -> bar becomes a file -> file edge; the self-call foo -> x.py folds
    # away with the endpoints merged; the original imports edge dedupes into
    # the same (src, dst, type).
    calls = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == EDGE_CALLS}
    assert calls == {("api:f:src/x.py", "api:f:src/y.py")}
    imports = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == EDGE_IMPORTS}
    assert imports == {("api:f:src/x.py", "api:f:src/y.py")}


def test_overview_keeps_sql_objects_as_tables(tmp_path: Path):
    _clone(tmp_path, "db")
    _write_graph(tmp_path, "db", _sql_graph())
    payload = project.aggregate_project_graph(str(_fs(tmp_path)), docs=[], level="overview")
    by_id = {node["id"]: node for node in payload["nodes"]}
    table = by_id.get("db:s:db/schema.sql:dbo.Orders")
    assert table is not None and table["type"] == "table"
    assert "db:s:db/schema.sql:dbo.Orders.OrderId" not in by_id  # column deferred
    fk = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == EDGE_FOREIGN_KEY}
    # Table-to-table fk stays between the tables — NOT rolled into the file.
    assert fk == {("db:s:db/schema.sql:dbo.Orders", "db:s:db/schema.sql:dbo.Customers")}
    reads = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == EDGE_READS}
    # The proc is structural (kept at overview), so the edge keeps its
    # procedure endpoint instead of rolling up to the .sql file.
    assert reads == {("db:s:db/procs/GetOrder.sql:dbo.GetOrder",
                      "db:s:db/schema.sql:dbo.Orders")}


def test_full_level_emits_symbols(tmp_path: Path):
    payload = project.aggregate_project_graph(str(_fs(tmp_path)), docs=[], level="full")
    ids = {node["id"] for node in payload["nodes"]}
    assert "api:s:src/x.py:foo" in ids
    assert "api:s:src/y.py:bar" in ids
    calls = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == EDGE_CALLS}
    assert ("api:s:src/x.py:foo", "api:s:src/y.py:bar") in calls


def test_doc_nodes_and_reference_edges(tmp_path: Path):
    payload = project.aggregate_project_graph(str(_fs(tmp_path)), docs=DOCS)
    by_id = {node["id"]: node for node in payload["nodes"]}
    doc = by_id["d:domain/context"]
    assert doc["type"] == "module" and doc["repo"] is None
    assert doc["name"] == "项目背景" and doc["category"] == "domain"
    refs = {(e["src"], e["dst"]) for e in payload["edges"] if e["type"] == "references"}
    # missing/slug is dangling and must not produce an edge.
    assert refs == {("d:domain/context", "d:technical/api"),
                    ("d:technical/api", "d:domain/context")}
    assert payload["counts"]["docs"] == 2


def test_max_nodes_truncation_prefers_structural(tmp_path: Path):
    # 150 files each with a symbol: 150 structural + 150 symbols. max_nodes
    # clamps to [200, 5000], so the budget must sit above MIN to bite.
    nodes = [GraphNode(id=f"f:m{i}.py", type="file", name=f"m{i}.py") for i in range(150)]
    nodes += [
        GraphNode(id=f"s:m{i}.py:fn{i}", type="function", name=f"fn{i}")
        for i in range(150)
    ]
    _clone(tmp_path, "big")
    _write_graph(tmp_path, "big", RepoGraph(
        repo=RepoMeta(name="big", head_sha="", langs=["python"], built_at=0.0),
        nodes=nodes, edges=[],
    ))
    payload = project.aggregate_project_graph(str(tmp_path), docs=[], level="full",
                                              max_nodes=220)
    assert payload["truncated"] is True
    ids = [node["id"] for node in payload["nodes"]]
    assert len(ids) == 220
    # Structural first: all 150 files survive, only 70 symbols fit.
    assert sum(1 for node_id in ids if node_id.startswith("big:f:")) == 150
    assert sum(1 for node_id in ids if node_id.startswith("big:s:")) == 70


def test_needs_build_and_no_repos(tmp_path: Path):
    _clone(tmp_path, "lonely")  # clone without any graph.json
    payload = project.aggregate_project_graph(str(tmp_path), docs=[])
    assert payload["needs_build"] is True
    assert payload["repos"] == [{"name": "lonely", "has_graph": False}]
    assert payload["nodes"] == [] and payload["edges"] == []

    empty = project.aggregate_project_graph(str(tmp_path / "nowhere"), docs=DOCS)
    assert empty["needs_build"] is False
    assert empty["no_repos"] is True
    assert empty["counts"]["docs"] == 2  # docs still render without any repo


def test_list_repo_statuses(tmp_path: Path):
    statuses = project.list_repo_statuses(str(_fs(tmp_path)))
    assert [entry["name"] for entry in statuses] == ["api", "web"]
    assert statuses[0]["stats"] == {"files": 2, "nodes": 5, "edges": 4}
    assert statuses[0]["head_sha"] == "abc"


def test_graph_node_neighbors_namespaced(tmp_path: Path):
    fs = _fs(tmp_path)
    result = project.graph_node_neighbors(str(fs), "api", "s:src/x.py:foo", depth=1)
    assert result is not None
    assert result["node"]["id"] == "api:s:src/x.py:foo"
    neighbor_ids = {entry["id"] for entry in result["neighbors"]}
    assert "api:s:src/y.py:bar" in neighbor_ids
    assert all(entry["repo"] == "api" for entry in result["neighbors"])
    edge_ids = {(e["src"], e["dst"]) for e in result["edges"]}
    assert ("api:s:src/x.py:foo", "api:s:src/y.py:bar") in edge_ids


def test_graph_node_neighbors_unknown(tmp_path: Path):
    fs = _fs(tmp_path)
    assert project.graph_node_neighbors(str(fs), "ghost", "f:x.py") is None
    assert project.graph_node_neighbors(str(fs), "api", "f:ghost.py") is None


def test_invalid_level_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        project.aggregate_project_graph(str(tmp_path), docs=[], level="deep")
