"""Query helpers over a loaded RepoGraph.

Pure functions — no I/O, no mutation — shared by the repo_graph tool and
tests. Ambiguity is reported, never guessed: a qname that exists in several
files returns both candidates so the caller can disambiguate.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from .model import RepoGraph, is_symbol_id, parse_node_id

MAX_DEPTH = 2
MAX_HOPS = 6
SEARCH_LIMIT = 20


def _entry(graph: RepoGraph, node_id: str, **extra: Any) -> dict[str, Any]:
    node = graph.node(node_id)
    kind, rel, _ = parse_node_id(node_id)
    return {
        "node_id": node_id,
        "name": node.name,
        "type": node.type,
        "rel": rel,
        "line": node.line,
        **extra,
    }


def resolve_node(graph: RepoGraph, query: str) -> dict[str, Any]:
    """Resolve a node id, file path, or symbol qname to one node.

    Order: exact node id -> file path (exact or directory prefix) -> symbol
    qname exact -> symbol suffix. Multiple hits are returned as ambiguous
    rather than guessed.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required"}
    node = graph.node(q)
    if node:
        return _entry(graph, q)

    rel_matches = [
        n for n in graph.nodes
        if n.type == "file" and (n.name == q or n.name.startswith(q + "/"))
    ]
    if len(rel_matches) == 1:
        return _entry(graph, rel_matches[0].id)
    if len(rel_matches) > 1:
        return {"ambiguous": [n.id for n in rel_matches],
                "error": f"{len(rel_matches)} files match {q!r}"}

    exact = [n for n in graph.nodes if is_symbol_id(n.id) and n.name == q]
    if len(exact) == 1:
        return _entry(graph, exact[0].id)
    if len(exact) > 1:
        return {"ambiguous": [n.id for n in exact], "error": f"ambiguous symbol {q!r}"}

    suffix = [n for n in graph.nodes if is_symbol_id(n.id) and n.name.endswith("." + q)]
    if len(suffix) == 1:
        return _entry(graph, suffix[0].id)
    if len(suffix) > 1:
        return {"ambiguous": [n.id for n in suffix], "error": f"ambiguous symbol {q!r}"}
    return {"error": f"no node matches {q!r}"}


def neighbors(graph: RepoGraph, node_id: str, depth: int = 1) -> dict[str, Any]:
    """BFS in both directions; depth is capped at MAX_DEPTH."""
    depth = min(max(int(depth or 1), 1), MAX_DEPTH)
    fwd, rev = graph.adjacency()
    seen = {node_id}
    found: list[dict[str, Any]] = []
    frontier = [node_id]
    for level in range(1, depth + 1):
        next_frontier: list[str] = []
        for current in frontier:
            for etype, dst in fwd.get(current, []):
                if dst not in seen:
                    seen.add(dst)
                    found.append(_entry(graph, dst, direction="out", via=etype, depth=level))
                    next_frontier.append(dst)
            for etype, src in rev.get(current, []):
                if src not in seen:
                    seen.add(src)
                    found.append(_entry(graph, src, direction="in", via=etype, depth=level))
                    next_frontier.append(src)
        frontier = next_frontier
    return {"node_id": node_id, "depth": depth, "neighbors": found, "count": len(found)}


def impact_set(graph: RepoGraph, node_id: str) -> dict[str, Any]:
    """Reverse BFS from a node: every node that transitively depends on it.

    The refactor blast radius — 'what breaks if I change this'.
    """
    _, rev = graph.adjacency()
    seen = {node_id}
    queue: deque[str] = deque([node_id])
    affected: list[dict[str, Any]] = []
    while queue:
        current = queue.popleft()
        for etype, src in rev.get(current, []):
            if src not in seen:
                seen.add(src)
                affected.append(_entry(graph, src, via=etype))
                queue.append(src)
    files = sorted({parse_node_id(entry["node_id"])[1] for entry in affected})
    return {
        "node_id": node_id,
        "affected_nodes": affected,
        "affected_files": files,
        "count": len(files),
    }


def shortest_path(
    graph: RepoGraph, start: str, end: str, max_hops: int = MAX_HOPS
) -> dict[str, Any]:
    """Bidirectional BFS between two nodes; node-id path or an error."""
    from_query = resolve_node(graph, start)
    if "error" in from_query:
        return {"error": f"start: {from_query['error']}"}
    to_query = resolve_node(graph, end)
    if "error" in to_query:
        return {"error": f"end: {to_query['error']}"}
    start_id = from_query["node_id"]
    end_id = to_query["node_id"]
    if start_id == end_id:
        return {"path": [start_id], "hops": 0, "from": start_id, "to": end_id}

    max_hops = min(max(int(max_hops or MAX_HOPS), 1), MAX_HOPS)
    fwd, rev = graph.adjacency()
    parent_f: dict[str, str | None] = {start_id: None}
    parent_r: dict[str, str | None] = {end_id: None}
    frontier_f = {start_id}
    frontier_r = {end_id}
    meet: str | None = None

    for _hop in range(max_hops):
        next_f: set[str] = set()
        for current in frontier_f:
            for _etype, dst in fwd.get(current, []):
                if dst in parent_f:
                    continue
                parent_f[dst] = current
                if dst in parent_r:
                    meet = dst
                    break
                next_f.add(dst)
            if meet:
                break
        if meet:
            break
        frontier_f = next_f

        next_r: set[str] = set()
        for current in frontier_r:
            for _etype, src in rev.get(current, []):
                if src in parent_r:
                    continue
                parent_r[src] = current
                if src in parent_f:
                    meet = src
                    break
                next_r.add(src)
            if meet:
                break
        if meet:
            break
        frontier_r = next_r

    if meet is None:
        return {"error": f"no path between {start_id!r} and {end_id!r} within {max_hops} hops"}

    path_rev: list[str] = []
    node: str | None = meet
    while node is not None:
        path_rev.append(node)
        node = parent_f[node]
    path = path_rev[::-1]
    node = parent_r[meet]
    while node is not None:
        path.append(node)
        node = parent_r[node]
    return {"path": path, "hops": len(path) - 1, "from": start_id, "to": end_id}


def search(graph: RepoGraph, query: str, limit: int = SEARCH_LIMIT) -> dict[str, Any]:
    """Substring match over file rel paths and symbol qnames (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return {"error": "query is required"}
    matches: list[dict[str, Any]] = []
    for node in graph.nodes:
        if q not in node.name.lower():
            continue
        matches.append(_entry(graph, node.id))
    matches.sort(key=lambda entry: (entry["type"] != "file", entry["name"]))
    return {"query": query, "matches": matches[:limit], "count": len(matches[:limit])}
