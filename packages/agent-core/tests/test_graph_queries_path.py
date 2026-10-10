"""shortest_path hop limit: the bound must apply to the PATH length.

Bidirectional BFS expands one level from each side per iteration, so the
search covered ~2*max_hops hops and could return a path longer than the
caller's limit — while the failure message claimed a bound it did not honor.
"""
from __future__ import annotations

from agent_core.knowledge.graph import queries
from agent_core.knowledge.graph.model import (
    EDGE_FOREIGN_KEY,
    EDGE_IMPORTS,
    EDGE_PRIMARY_KEY,
    EDGE_READS,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
)


def _chain(n: int) -> RepoGraph:
    """f0.py -> f1.py -> ... -> f{n-1}.py (n-1 hops between the ends)."""
    nodes = [
        GraphNode(id=f"f:f{i}.py", type="file", name=f"f{i}.py")
        for i in range(n)
    ]
    edges = [
        GraphEdge(src=f"f:f{i}.py", dst=f"f:f{i + 1}.py", type=EDGE_IMPORTS)
        for i in range(n - 1)
    ]
    return RepoGraph(
        repo=RepoMeta(name="r", head_sha="", langs=[], built_at=0.0),
        nodes=nodes,
        edges=edges,
    )


def test_path_at_the_limit_is_returned():
    graph = _chain(4)
    result = queries.shortest_path(graph, "f0.py", "f3.py", max_hops=3)
    assert result["hops"] == 3
    assert result["path"] == ["f:f0.py", "f:f1.py", "f:f2.py", "f:f3.py"]


def test_path_over_the_limit_is_rejected():
    graph = _chain(4)  # 3 hops between the ends
    result = queries.shortest_path(graph, "f0.py", "f3.py", max_hops=2)
    assert "error" in result
    assert "within 2 hops" in result["error"]


def test_unreachable_pair_still_errors():
    graph = _chain(3)
    graph.nodes.append(GraphNode(id="f:island.py", type="file", name="island.py"))
    result = queries.shortest_path(graph, "f0.py", "island.py", max_hops=6)
    assert "error" in result


def _sql_graph() -> RepoGraph:
    """proc --reads--> Orders --fk--> Customers, plus a PK column -> Orders."""
    nodes = [
        GraphNode(id="f:db/schema.sql", type="file", name="db/schema.sql", lang="sql"),
        GraphNode(id="s:db/schema.sql:dbo.Orders", type="table", name="dbo.Orders"),
        GraphNode(id="s:db/schema.sql:dbo.Orders.OrderId", type="column",
                  name="dbo.Orders.OrderId"),
        GraphNode(id="s:db/schema.sql:dbo.Customers", type="table", name="dbo.Customers"),
        GraphNode(id="f:db/procs/GetOrder.sql", type="file", name="db/procs/GetOrder.sql", lang="sql"),
        GraphNode(id="s:db/procs/GetOrder.sql:dbo.GetOrder", type="procedure",
                  name="dbo.GetOrder"),
    ]
    edges = [
        GraphEdge(src="s:db/procs/GetOrder.sql:dbo.GetOrder",
                  dst="s:db/schema.sql:dbo.Orders", type=EDGE_READS),
        GraphEdge(src="s:db/schema.sql:dbo.Orders",
                  dst="s:db/schema.sql:dbo.Customers", type=EDGE_FOREIGN_KEY),
        GraphEdge(src="s:db/schema.sql:dbo.Orders.OrderId",
                  dst="s:db/schema.sql:dbo.Orders", type=EDGE_PRIMARY_KEY),
    ]
    return RepoGraph(
        repo=RepoMeta(name="r", head_sha="", langs=["sql"], built_at=0.0),
        nodes=nodes,
        edges=edges,
    )


def test_impact_crosses_sql_edges():
    """impact_set walks schema edges (fk/pk) and code edges (reads) alike,
    and schema objects resolve by their plain qname."""
    graph = _sql_graph()
    resolved = queries.resolve_node(graph, "dbo.Orders")
    assert resolved.get("node_id") == "s:db/schema.sql:dbo.Orders"
    assert queries.resolve_node(graph, "dbo.vOrders").get("error")  # only exact names

    # Blast radius on the parent table: fk reaches Orders, then reads + pk
    # fan out to the proc and the column — three edge types in one walk.
    impact = queries.impact_set(graph, "s:db/schema.sql:dbo.Customers")
    assert set(impact["affected_files"]) == {"db/schema.sql", "db/procs/GetOrder.sql"}
    vias = {entry.get("via") for entry in impact["affected_nodes"]}
    assert {EDGE_FOREIGN_KEY, EDGE_READS, EDGE_PRIMARY_KEY} <= vias
    assert impact["count"] == 2


def test_search_count_is_total_not_the_cap():
    """count is the TOTAL number of matches (the neighbors/impact_set
    convention) — len(matches[:limit]) reported the cap as the total whenever
    the result was truncated, under-reporting every match beyond the cap."""
    graph = _chain(4)  # f0.py..f3.py — every name matches "f"
    full = queries.search(graph, "f", limit=100)
    assert full["count"] == 4

    capped = queries.search(graph, "f", limit=2)
    assert len(capped["matches"]) == 2
    assert capped["count"] == 4
    assert capped.get("truncated") is True

    # At/under the cap nothing is cut, so no truncated flag.
    under = queries.search(graph, "f", limit=4)
    assert under["count"] == 4
    assert "truncated" not in under
