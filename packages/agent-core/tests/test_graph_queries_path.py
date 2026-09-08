"""shortest_path hop limit: the bound must apply to the PATH length.

Bidirectional BFS expands one level from each side per iteration, so the
search covered ~2*max_hops hops and could return a path longer than the
caller's limit — while the failure message claimed a bound it did not honor.
"""
from __future__ import annotations

from agent_core.knowledge.graph import queries
from agent_core.knowledge.graph.model import (
    EDGE_IMPORTS,
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
