"""Project-wide graph aggregation: every checkout's graph + knowledge docs.

Serves the 图谱 tab. Stored graph.json files keep repo-relative node ids;
this module namespaces them ONLY on output — code nodes become
``{repo}:{raw_id}`` (e.g. ``api:f:src/a.py``), knowledge-page nodes become
``d:{slug}`` — so two checkouts with the same ``f:src/x.py`` never collide.
Every emitted node carries a ``repo`` field (None for docs) so consumers
filter without parsing ids. The neighbors API reuses the same contract in
reverse: strip the repo prefix, query the per-repo graph, re-namespace.

Like the rest of knowledge/graph, no dependency on the tools package or the
DB: doc rows arrive as plain dicts from the API layer.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .builder import build_repo_graph
from .model import (
    NODE_CLASS,
    NODE_COLUMN,
    NODE_FILE,
    NODE_FUNCTION,
    NODE_MODULE,
    NODE_PROCEDURE,
    NODE_SYMBOL,
    NODE_TABLE,
    NODE_TRIGGER,
    NODE_VIEW,
    RepoGraph,
    parse_node_id,
    repo_graph_dir,
)
from .queries import NEIGHBOR_LIMIT, neighbors as query_neighbors
from .store import load_cached

_log = logging.getLogger(__name__)

DEFAULT_MAX_NODES = 1500
MIN_MAX_NODES = 200
MAX_MAX_NODES = 5000

# Overview keeps the structural layer only: files, SQL objects, docs. Symbol
# nodes (class/function/symbol/column) surface on demand (full level or the
# neighbors call), so a 3000-file repo paints as ~thousands of nodes, not tens
# of thousands.
_OVERVIEW_TYPES = frozenset({
    NODE_FILE, NODE_TABLE, NODE_VIEW, NODE_PROCEDURE, NODE_TRIGGER, NODE_MODULE,
})
# Cap priority when truncated: structural nodes emit first (they carry the
# architecture), symbol nodes fill whatever budget remains. Column is
# symbol-class: same s:-prefixed ids, expanded on demand next to its table.
_SYMBOL_TYPES = frozenset({NODE_CLASS, NODE_FUNCTION, NODE_SYMBOL, NODE_COLUMN})
# Every node type the graph can emit; the `types` filter whitelist validates
# against this.
KNOWN_NODE_TYPES = _OVERVIEW_TYPES | _SYMBOL_TYPES

_DOC_ID_PREFIX = "d:"


def list_clones(project_fs_path: str) -> list[str]:
    """Names of checkouts under ``{project}/repos`` (directories with .git).

    Inlined from tools/_repo_paths: knowledge/graph must stay importable
    standalone (same rule builder.py follows for the walk/containment logic).
    """
    directory = Path(project_fs_path).resolve() / "repos"
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and (entry / ".git").exists()
    )


def _load_project_graphs(project_fs_path: str) -> list[tuple[str, RepoGraph | None]]:
    """(repo_name, graph|None) for each clone, in name order.

    load_cached is mtime-keyed, so repeated calls on an unchanged graph.json
    cost one dict lookup; a rebuild invalidates through builder's
    invalidate_cached call.
    """
    out: list[tuple[str, RepoGraph | None]] = []
    for name in list_clones(project_fs_path):
        kg_dir = repo_graph_dir(project_fs_path, name)
        graph = load_cached(kg_dir)
        out.append((name, graph))
    return out


def list_repo_statuses(project_fs_path: str) -> list[dict[str, Any]]:
    """Per-clone build status chips for the UI (no node payload)."""
    statuses: list[dict[str, Any]] = []
    for name, graph in _load_project_graphs(project_fs_path):
        if graph is None:
            statuses.append({"name": name, "has_graph": False})
            continue
        stats = graph.stats or {}
        statuses.append({
            "name": name,
            "has_graph": True,
            "built_at": graph.repo.built_at,
            "head_sha": graph.repo.head_sha,
            "stats": {
                "files": stats.get("files", 0),
                "nodes": stats.get("nodes", 0),
                "edges": stats.get("edges", 0),
            },
        })
    return statuses


async def build_project_graphs(
    project_fs_path: str, force: bool = False
) -> list[dict[str, Any]]:
    """Build/refresh every clone's graph; per-repo summaries in name order.

    Sequential on purpose: each build takes CPU and the per-kg_dir lock inside
    build_repo_graph serializes same-repo builds anyway — parallelizing across
    repos would just fight for the GIL/thread pool. Oversized repos come back
    as status=skipped/too_many_files from the existing guard, not an error.
    """
    summaries: list[dict[str, Any]] = []
    for name in list_clones(project_fs_path):
        repo = Path(project_fs_path).resolve() / "repos" / name
        kg_dir = repo_graph_dir(project_fs_path, name)
        try:
            summary = await build_repo_graph(repo, kg_dir, force=force)
        except Exception as exc:  # one bad repo must not fail the whole build
            _log.exception("project graph build failed for %s", name)
            summary = {"status": "failed", "reason": str(exc)}
        summaries.append({"name": name, **summary})
    return summaries


def _doc_node(doc: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": _DOC_ID_PREFIX + str(doc.get("slug", "")),
        "repo": None,
        "type": NODE_MODULE,
        "name": str(doc.get("title") or doc.get("slug") or ""),
        "slug": str(doc.get("slug", "")),
        "category": str(doc.get("category") or ""),
    }
    if doc.get("summary"):
        node["summary"] = str(doc["summary"])
    return node


def _code_node(node: Any, repo: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": f"{repo}:{node.id}",
        "repo": repo,
        "type": node.type,
        "name": node.name,
    }
    if node.lang:
        out["lang"] = node.lang
    if node.line:
        out["line"] = node.line
    if node.lines:
        out["lines"] = node.lines
    return out


def _rollup(raw_id: str, level: str, type_by_id: dict[str, str]) -> str:
    """Overview maps symbol-class nodes to their owning file id.

    Type-based, never id-based: SQL objects (table/view/procedure/trigger)
    also live under ``s:`` ids, and rolling those into their .sql file would
    erase the schema layer from the overview. Containment is implied by the
    id, so no edge lookup is needed for the rewrite itself.
    """
    if level != "overview":
        return raw_id
    ntype = type_by_id.get(raw_id)
    if ntype is None or ntype in _OVERVIEW_TYPES:
        return raw_id  # structural, or unknown endpoint (final filter drops it)
    _, rel, _ = parse_node_id(raw_id)
    return "f:" + rel


def graph_edge_expansion(
    project_fs_path: str, repo: str, raw_src: str, raw_dst: str, etype: str | None = None
) -> dict[str, Any] | None:
    """Raw (symbol-level) edges that roll up into one overview edge.

    The overview folds ``s:a:foo -calls-> s:b:bar`` into ``f:a -calls-> f:b``
    and drops the symbols, and BFS from the file endpoint cannot recover them
    (containment is id-derived, never an edge — the file has no traversal
    path to its own symbols). So expansion matches on the rollup itself:
    every raw edge whose rolled endpoints equal the clicked pair. None when
    the repo/graph is unknown or either endpoint is not a node of it.

    Response mirrors the neighbors contract (namespaced ids, nodes carried
    along) so the canvas merges both the same way.
    """
    graph = load_cached(repo_graph_dir(project_fs_path, repo))
    if graph is None:
        return None
    type_by_id = {node.id: node.type for node in graph.nodes}
    if raw_src not in type_by_id or raw_dst not in type_by_id:
        return None

    matched: list[Any] = []
    endpoint_ids: dict[str, None] = {}
    for edge in graph.edges:
        if etype and edge.type != etype:
            continue
        if _rollup(edge.src, "overview", type_by_id) != raw_src:
            continue
        if _rollup(edge.dst, "overview", type_by_id) != raw_dst:
            continue
        matched.append(edge)
        endpoint_ids.setdefault(edge.src, None)
        endpoint_ids.setdefault(edge.dst, None)
        if len(matched) >= NEIGHBOR_LIMIT:
            break

    nodes: list[dict[str, Any]] = []
    for raw_id in endpoint_ids:
        node = graph.node(raw_id)
        if node is None:
            continue
        entry = _code_node(node, repo)
        entry["direction"] = None
        entry["via"] = None
        entry["depth"] = None
        nodes.append(entry)
    return {
        "src": f"{repo}:{raw_src}",
        "dst": f"{repo}:{raw_dst}",
        "count": len(matched),
        "edges": [
            {"src": f"{repo}:{edge.src}", "dst": f"{repo}:{edge.dst}",
             "type": edge.type}
            for edge in matched
        ],
        "nodes": nodes,
        **({"truncated": True} if len(matched) >= NEIGHBOR_LIMIT else {}),
    }


def aggregate_project_graph(
    project_fs_path: str,
    docs: list[dict[str, Any]],
    *,
    level: str = "overview",
    max_nodes: int = DEFAULT_MAX_NODES,
    types: Collection[str] | None = None,
) -> dict[str, Any]:
    """Merge every repo graph with knowledge-doc nodes into one payload.

    ``docs`` items: {slug, title, summary, category, cross_references} —
    plain dicts, sourced from KnowledgeMetadata by the API layer. Doc
    ``references`` edges only land when both slugs resolve to emitted doc
    nodes; dangling slugs are dropped.

    ``types`` whitelists node types: when non-empty it replaces the level
    filter for node emission (selected symbol types surface even at
    overview) and truncation then budget-limits only the selected set.
    Edge rollup still keys off ``level``; with an active filter, raw
    (symbol-level) edges survive as a fallback whenever their rolled form
    is dropped for want of unselected endpoints — otherwise a symbol-only
    selection would paint isolated nodes. Empty/absent ``types`` keeps the
    legacy behavior byte-for-byte. ``type_counts`` is computed from the raw
    graphs BEFORE any level/type/truncation filter: the UI builds its type
    chips from it, so truncated-out or overview-hidden types stay
    discoverable — do not "fix" it to count emitted nodes.
    """
    if level not in ("overview", "full"):
        raise ValueError(f"invalid level: {level!r}")
    if types:
        unknown = set(types) - KNOWN_NODE_TYPES
        if unknown:
            raise ValueError(f"invalid types: {sorted(unknown)!r}")
    type_filter = frozenset(types) if types else None
    max_nodes = max(MIN_MAX_NODES, min(int(max_nodes or DEFAULT_MAX_NODES), MAX_MAX_NODES))

    loaded = _load_project_graphs(project_fs_path)
    has_clones = bool(loaded)
    needs_build = has_clones and all(graph is None for _, graph in loaded)

    repos: list[dict[str, Any]] = []
    structural: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    # (src, dst, type) in output ids; order-preserving, deduped on insert.
    edge_keys: dict[tuple[str, str, str], None] = {}
    # Raw pre-rollup counterparts, collected only when a type filter is
    # active at overview (full level already emits raw endpoints).
    raw_fallback = type_filter is not None and level == "overview"
    raw_edge_keys: dict[tuple[str, str, str], None] = {}
    # Filter-independent per-type node totals for the UI chips.
    type_counts: dict[str, int] = {}

    for name, graph in loaded:
        if graph is None:
            repos.append({"name": name, "has_graph": False})
            continue
        stats = graph.stats or {}
        repos.append({
            "name": name,
            "has_graph": True,
            "built_at": graph.repo.built_at,
            "head_sha": graph.repo.head_sha,
            "stats": {
                "files": stats.get("files", 0),
                "nodes": stats.get("nodes", 0),
                "edges": stats.get("edges", 0),
            },
        })
        type_by_id = {node.id: node.type for node in graph.nodes}
        for node in graph.nodes:
            type_counts[node.type] = type_counts.get(node.type, 0) + 1
            is_structural = node.type in _OVERVIEW_TYPES
            if type_filter is not None:
                if node.type not in type_filter:
                    continue  # whitelist; selected symbols bypass the overview drop
            elif level == "overview" and not is_structural:
                continue
            entry = _code_node(node, name)
            (structural if is_structural else symbols).append(entry)
        # Roll up BEFORE filtering: at overview a symbol-symbol edge becomes a
        # file-file edge and must survive even though the symbols themselves
        # are not emitted. Endpoints that still miss the emitted set drop at
        # the final emitted_ids filter.
        for edge in graph.edges:
            src = f"{name}:{_rollup(edge.src, level, type_by_id)}"
            dst = f"{name}:{_rollup(edge.dst, level, type_by_id)}"
            if src != dst:
                edge_keys.setdefault((src, dst, edge.type), None)
            if raw_fallback:
                raw_src = f"{name}:{edge.src}"
                raw_dst = f"{name}:{edge.dst}"
                if raw_src != raw_dst:
                    raw_edge_keys.setdefault((raw_src, raw_dst, edge.type), None)

    doc_nodes = [_doc_node(doc) for doc in docs if doc.get("slug")]
    if doc_nodes:  # no module key at all when there are no docs (no empty chip)
        type_counts[NODE_MODULE] = type_counts.get(NODE_MODULE, 0) + len(doc_nodes)
    if type_filter is not None and NODE_MODULE not in type_filter:
        doc_nodes = []
    structural.extend(doc_nodes)
    doc_ids = {node["id"] for node in doc_nodes}
    for doc in docs:
        src_id = _DOC_ID_PREFIX + str(doc.get("slug", ""))
        for target in doc.get("cross_references") or []:
            dst_id = _DOC_ID_PREFIX + str(target)
            if dst_id in doc_ids and src_id != dst_id:
                edge_keys.setdefault((src_id, dst_id, "references"), None)

    truncated = len(structural) + len(symbols) > max_nodes
    nodes = structural[:max_nodes]
    if len(nodes) < max_nodes:
        nodes.extend(symbols[: max_nodes - len(nodes)])
    emitted_ids = {node["id"] for node in nodes}
    # Raw fallback candidates merge in after the rolled edges (dedup keeps a
    # file-file raw edge that already exists in rolled form from doubling).
    candidates: dict[tuple[str, str, str], None] = edge_keys
    if raw_fallback:
        candidates = dict(edge_keys)
        for key in raw_edge_keys:
            candidates.setdefault(key, None)
    edges = [
        {"src": src, "dst": dst, "type": etype}
        for (src, dst, etype) in candidates
        if src in emitted_ids and dst in emitted_ids
    ]

    return {
        "needs_build": needs_build,
        "generated_at": time.time(),
        "level": level,
        "truncated": truncated,
        "no_repos": not has_clones,
        "repos": repos,
        "type_counts": type_counts,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "docs": len(doc_nodes),
            "repos": sum(1 for repo in repos if repo.get("has_graph")),
        },
        "nodes": nodes,
        "edges": edges,
    }


def graph_node_neighbors(
    project_fs_path: str, repo: str, raw_id: str, depth: int = 1
) -> dict[str, Any] | None:
    """Namespaced neighbors of one code node; None when repo/graph/node unknown.

    The center node is included as entry 0 with direction=None so the client
    can seed its detail panel from one response. Edges among the returned set
    are included so the canvas can draw the expanded neighborhood.
    """
    graph = load_cached(repo_graph_dir(project_fs_path, repo))
    if graph is None or graph.node(raw_id) is None:
        return None
    result = query_neighbors(graph, raw_id, depth=depth)
    entries: list[dict[str, Any]] = []
    center = graph.node(raw_id)
    assert center is not None
    entries.append({
        "id": f"{repo}:{raw_id}",
        "repo": repo,
        "type": center.type,
        "name": center.name,
        "lang": center.lang,
        "line": center.line,
        "lines": center.lines,
        "direction": None,
        "via": None,
        "depth": 0,
    })
    seen = {raw_id}
    for entry in result["neighbors"]:
        node = graph.node(entry["node_id"])
        if node is None:
            continue
        seen.add(entry["node_id"])
        entries.append({
            "id": f"{repo}:{entry['node_id']}",
            "repo": repo,
            "type": node.type,
            "name": node.name,
            "lang": node.lang,
            "line": node.line,
            "lines": node.lines,
            "direction": entry.get("direction"),
            "via": entry.get("via"),
            "depth": entry.get("depth"),
        })
    edges = [
        {"src": f"{repo}:{edge.src}", "dst": f"{repo}:{edge.dst}", "type": edge.type}
        for edge in graph.edges
        if edge.src in seen and edge.dst in seen
    ]
    return {
        "node": entries[0],
        "neighbors": entries[1:],
        "count": result["count"],
        "edges": edges,
        **({"truncated": True} if result.get("truncated") else {}),
    }
