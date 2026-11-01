"""Sole import point for the graphify library (PyPI ``graphifyy``).

Every graphify call in agent-core goes through this module so upstream API
churn is confined to one file. The builder orchestrates it; model/store/
queries keep our own schema-v2 contract and never see graphify types.

Three upstream constraints shape everything here (verified against v0.9.100):

- ``graphify.extractors.base._ACTIVE_SCAN_ROOT`` is a module-level global,
  not thread-local, and graphify itself warns (#3989) that crossing cache
  roots within one process corrupts its fast path — so every graphify call
  runs under a process-wide lock, with a per-repo ``cache_root``.
- ``GRAPHIFY_OUT`` is read once at import time; we never set it. Output and
  cache locations are always passed explicitly per call.
- ``extract(parallel=True)`` spawns a ProcessPoolExecutor, which dies under
  uvicorn's ``__main__`` on Windows spawn — always ``parallel=False``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import (
    EDGE_CALLS,
    EDGE_FOREIGN_KEY,
    EDGE_IMPORTS,
    EDGE_INHERITS,
    EDGE_PRIMARY_KEY,
    EDGE_READS,
    EDGE_REFERENCES,
    EDGE_WRITES,
    NODE_CLASS,
    NODE_FILE,
    NODE_FUNCTION,
    NODE_SYMBOL,
    NODE_TABLE,
    GraphEdge,
    GraphNode,
    RepoGraph,
    RepoMeta,
    file_id,
    now_epoch,
    symbol_id,
)

_log = logging.getLogger(__name__)

# One process-wide lock serializes all graphify calls: the scan-root global
# and the per-process stat index are not re-entrant across roots (see module
# docstring). Callers already run us inside asyncio.to_thread, so the lock
# never touches the event loop.
_GRAPHIFY_LOCK = threading.RLock()

# graphify relations we do not translate into schema-v2 edges: containment is
# derived from our node ids, never stored as edges (model.py).
_SKIP_RELATIONS = frozenset({"contains", "method"})

# graphify relation -> schema-v2 edge type. Unknown relations degrade to
# EDGE_REFERENCES so traversal (neighbors/impact/path) still crosses them.
_RELATION_MAP: dict[str, str] = {
    "imports": EDGE_IMPORTS,
    "imports_from": EDGE_IMPORTS,
    "calls": EDGE_CALLS,
    "inherits": EDGE_INHERITS,
    "reads_from": EDGE_READS,
    "reads": EDGE_READS,
    "writes_to": EDGE_WRITES,
    "writes": EDGE_WRITES,
    "foreign_key": EDGE_FOREIGN_KEY,
    "primary_key": EDGE_PRIMARY_KEY,
    "references": EDGE_REFERENCES,
    "uses": EDGE_REFERENCES,
}

_SQL_SUFFIX = ".sql"

# Cached lazy-import state; guarded by _GRAPHIFY_LOCK.
_IMPORTS: dict[str, Any] = {}
_IMPORT_MS = 0


@dataclass
class GraphifyResult:
    """Outcome of one extract+build pass over a candidate file list."""

    graph: Any                      # nx.Graph (networkx), post-build
    extracted: int                  # files freshly extracted this pass
    reused: int = 0                 # files served from graphify's SHA256 cache
    failed_sources: list[str] = field(default_factory=list)
    skipped_sources: list[str] = field(default_factory=list)
    import_ms: int = 0


class GraphifyUnavailable(RuntimeError):
    """graphifyy is not installed (or its API drifted); build must degrade."""


def _ensure_imported() -> dict[str, Any]:
    """Import graphify on first use; cached thereafter.

    Runs under _GRAPHIFY_LOCK (callers' contract) — the import itself is not
    thread-safe against a concurrent extract() from another root.
    """
    global _IMPORT_MS
    if _IMPORTS:
        return _IMPORTS
    started = time.perf_counter()
    try:
        from graphify.analyze import god_nodes, surprising_connections
        from graphify.build import build
        from graphify.cache import load_cached
        from graphify.cluster import cluster, label_communities_by_hub, score_all
        from graphify.extract import extract
        from graphify.rcfile import cache_salt
    except ImportError as exc:  # pragma: no cover - env without the dependency
        raise GraphifyUnavailable(
            f"graphifyy is not importable ({exc}); install agent-core with its "
            "declared dependencies (graphifyy[sql]==0.9.100)"
        ) from exc
    _IMPORTS.update({
        "build": build,
        "cluster": cluster,
        "score_all": score_all,
        "label_communities_by_hub": label_communities_by_hub,
        "god_nodes": god_nodes,
        "surprising_connections": surprising_connections,
        "extract": extract,
        "load_cached": load_cached,
        "cache_salt": cache_salt,
    })
    _IMPORT_MS = int((time.perf_counter() - started) * 1000)
    return _IMPORTS


# Suffixes graphify never reads/writes its AST cache for (extract always
# re-parses them) — a cache probe must not count them as hits. Mirrors
# graphify.extractors.models._JS_CACHE_BYPASS_SUFFIXES.
_CACHE_BYPASS_SUFFIXES = frozenset({
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".ejs",
})


def _count_cache_hits(api: dict[str, Any], paths: list[Path],
                      root: Path, cache_root: Path) -> int:
    """Files extract() will serve from its SHA256 cache instead of re-parsing.

    extract()'s ``extracted_sources`` lists cache hits too, so the builder's
    parsed/reused split needs its own probe — same load_cached call extract
    makes internally, before it runs (its own probes then hit the stat index
    and cost ~nothing).
    """
    hits = 0
    for path in paths:
        if path.suffix.lower() in _CACHE_BYPASS_SUFFIXES:
            continue
        try:
            cached = api["load_cached"](
                path, root, cache_root=cache_root, salt=api["cache_salt"](path)
            )
        except OSError:
            continue
        if cached is not None:
            hits += 1
    return hits


def build_graph(paths: list[Path], *, root: Path, cache_root: Path) -> GraphifyResult:
    """Extract + build one repo's graph. Blocking — caller wraps in to_thread.

    ``paths`` must already be filtered to code extensions and regular files
    inside ``root``; we pass them through verbatim so the builder keeps
    control over candidate selection (size caps, git tracking rules).
    """
    with _GRAPHIFY_LOCK:
        api = _ensure_imported()
        reused = _count_cache_hits(api, paths, root, cache_root)
        extraction = api["extract"](
            paths, cache_root=cache_root, root=root, parallel=False
        )
        graph = api["build"]([extraction], directed=False, root=root)
    return GraphifyResult(
        graph=graph,
        extracted=max(len(paths) - reused - len(extraction.get("failed_sources") or []), 0),
        reused=reused,
        failed_sources=list(extraction.get("failed_sources") or []),
        skipped_sources=list(extraction.get("skipped_sources") or []),
        import_ms=_IMPORT_MS,
    )


def _clean_label(label: str) -> str:
    """Symbol label as a qname segment: no call parens, no leading-dot spam."""
    text = str(label or "").strip()
    if text.endswith("()"):
        text = text[:-2]
    return text.lstrip(".")


def _is_file_node(label: str, rel: str) -> bool:
    """True for graphify's file nodes.

    With unique basenames the label is just the basename; when two files in
    different dirs share a basename, build() rewrites the label to the full
    repo-relative path to keep node labels unique — both spell the file, so
    either match identifies the node.
    """
    return bool(rel) and label in (os.path.basename(rel), rel)


def _derive_qnames(graph: Any) -> dict[str, str]:
    """Class-qualified qname per symbol node, from contains/method parents.

    graphify labels methods with a bare ``.run()`` or a class-prefixed
    ``Impl.Go()``; our resolve_node matches on dotted qnames (``Service.run``),
    so walk the containment chain and join segments that are not already
    present. File parents stop the walk (they are not qname segments).
    """
    parents: dict[str, str] = {}
    for u, v, data in graph.edges(data=True):
        if data.get("relation") in ("contains", "method"):
            parents[v] = u

    qnames: dict[str, str] = {}

    def _file_rel(node_id: str) -> str | None:
        data = graph.nodes[node_id]
        source = str(data.get("source_file") or "")
        label = str(data.get("label") or "")
        return source if _is_file_node(label, source) else None

    def _qname(node_id: str, depth: int = 0) -> str:
        cached = qnames.get(node_id)
        if cached is not None:
            return cached
        data = graph.nodes[node_id]
        own = _clean_label(data.get("label", ""))
        if depth >= 8:  # containment cycles must never hang the build
            qnames[node_id] = own
            return own
        parent = parents.get(node_id)
        if parent is None or _file_rel(parent) is not None or parent not in graph.nodes:
            qnames[node_id] = own
            return own
        parent_q = _qname(parent, depth + 1)
        combined = own if not parent_q or own.startswith(parent_q + ".") else f"{parent_q}.{own}"
        qnames[node_id] = combined
        return combined

    for node_id in graph.nodes:
        if _file_rel(node_id) is None:
            _qname(node_id)
    return qnames


def _node_type(data: dict[str, Any], rel: str) -> str:
    """Schema-v2 node type from graphify attributes."""
    if rel.endswith(_SQL_SUFFIX):
        # graphify's SQL extractor does not distinguish tables from views or
        # emit column nodes; tables are the common case.
        return NODE_TABLE
    if data.get("_callable_class"):
        return NODE_CLASS
    if data.get("_callable"):
        return NODE_FUNCTION
    return NODE_SYMBOL


def _unique_symbol_id(rel: str, qname: str, taken: set[str]) -> str:
    """``s:{rel}:{qname}``, salted with a stable hash on collision (never merged)."""
    base = symbol_id(rel, qname)
    if base not in taken:
        return base
    salt = hashlib.sha1(f"{rel}:{qname}".encode("utf-8")).hexdigest()[:8]
    candidate = f"{base}#{salt}"
    counter = 2
    while candidate in taken:
        candidate = f"{base}#{salt}{counter}"
        counter += 1
    return candidate


def to_repo_graph(
    result: GraphifyResult,
    *,
    repo_name: str,
    head_sha: str,
    candidates: list[str],
    langs: list[str],
    extra_stats: dict[str, Any] | None = None,
) -> RepoGraph:
    """Convert a graphify nx.Graph into our schema-v2 RepoGraph.

    Direction comes from the ``_src``/``_tgt`` arc markers, never from the
    undirected (u, v) ordering. Edges touching a skipped or unmapped node
    are dropped and counted as unresolved so traversal never hits a hole.
    """
    graph = result.graph
    qnames = _derive_qnames(graph)
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    id_map: dict[str, str] = {}
    taken: set[str] = set()
    lang_by_rel = {rel: lang_of(rel) for rel in candidates}
    files_with_symbols: set[str] = set()

    for node_id, data in graph.nodes(data=True):
        rel = str(data.get("source_file") or "").replace("\\", "/")
        if not rel:
            continue
        label = str(data.get("label") or "")
        if _is_file_node(label, rel) and not data.get("type"):
            # File node (SQL schema.sql has label == basename too; its type
            # attr is absent there, matching every other file node).
            new_id = file_id(rel)
            id_map[node_id] = new_id
            taken.add(new_id)
            nodes.append(GraphNode(
                id=new_id, type=NODE_FILE, name=os.path.basename(rel),
                lang=lang_by_rel.get(rel),
            ))
            continue
        qname = qnames.get(node_id) or _clean_label(label) or label
        if not qname:
            continue
        new_id = _unique_symbol_id(rel, qname, taken)
        id_map[node_id] = new_id
        taken.add(new_id)
        line = _line_of(data.get("source_location"))
        nodes.append(GraphNode(
            id=new_id, type=_node_type(data, rel), name=qname, line=line,
        ))
        files_with_symbols.add(rel)

    unresolved = 0
    seen_edges: set[tuple[str, str, str]] = set()
    for u, v, data in graph.edges(data=True):
        relation = str(data.get("relation") or "")
        if relation in _SKIP_RELATIONS:
            continue
        # The nx.Graph is undirected: (u, v) order is arbitrary. Arc direction
        # lives in the _src/_tgt markers stamped by build() (graphify #563).
        src = id_map.get(str(data.get("_src", u)))
        dst = id_map.get(str(data.get("_tgt", v)))
        if src is None or dst is None:
            unresolved += 1
            continue
        edge_type = _RELATION_MAP.get(relation, EDGE_REFERENCES)
        if edge_type == EDGE_REFERENCES and relation == "references":
            src_rel = parse_rel(src)
            dst_rel = parse_rel(dst)
            if src_rel and src_rel == dst_rel and src_rel.endswith(_SQL_SUFFIX):
                # SQL table -> table `references` edge is the foreign key.
                edge_type = EDGE_FOREIGN_KEY
        key = (src, dst, edge_type)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(GraphEdge(src=src, dst=dst, type=edge_type))

    lang_coverage: dict[str, dict[str, int]] = {}
    for rel in candidates:
        lang = lang_by_rel.get(rel) or "other"
        cov = lang_coverage.setdefault(lang, {"files": 0, "with_symbols": 0})
        cov["files"] += 1
        if rel in files_with_symbols:
            cov["with_symbols"] += 1

    stats: dict[str, Any] = {
        "files": len(candidates),
        "nodes": len(nodes),
        "edges": len(edges),
        "parsed": result.extracted,
        "reused": result.reused,
        "failed": len(result.failed_sources),
        "unresolved_calls": unresolved,
        "with_symbols": len(files_with_symbols),
        "lang_coverage": lang_coverage,
    }
    if result.failed_sources:
        stats["failed_reasons"] = {"graphify extract failed": len(result.failed_sources)}
    if extra_stats:
        stats.update(extra_stats)

    return RepoGraph(
        repo=RepoMeta(
            name=repo_name, head_sha=head_sha, langs=sorted(langs),
            built_at=now_epoch(),
        ),
        stats=stats,
        nodes=nodes,
        edges=edges,
    )


def parse_rel(node_id: str) -> str | None:
    """Repo-relative path carried by a schema-v2 node id (None for symbols
    whose qname contains no path separator pattern we trust)."""
    if node_id.startswith("f:"):
        return node_id[2:]
    if node_id.startswith("s:"):
        rest = node_id[2:]
        rel, sep, _ = rest.partition(":")
        return rel if sep else None
    return None


def _line_of(source_location: Any) -> int:
    """``\"L42\"`` -> 42; anything else -> 0."""
    text = str(source_location or "")
    if text.startswith("L") and text[1:].isdigit():
        return int(text[1:])
    return 0


_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "jsx",
    ".vue": "vue",
    ".java": "java",
    ".cs": "csharp",
    ".sql": "sql",
}


def lang_of(rel: str) -> str | None:
    """Reporting-language label for a repo-relative path (None when we do
    not track one — coverage stats bucket those as "other")."""
    return _EXT_LANG.get(Path(rel).suffix.lower())
