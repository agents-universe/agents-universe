"""Graph data model and JSON codec.

Containment is derived from node ids, never stored as edges: a symbol id
``s:{rel}:{qname}`` implies its file ``f:{rel}``. Keeps the edge list free
of ~1 redundant edge per node.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# stats keys (also written into graph.json "stats")
STAT_FILES = "files"            # tracked source files in the repo
STAT_NODES = "nodes"
STAT_EDGES = "edges"
STAT_PARSED = "parsed"          # files freshly parsed this build
STAT_REUSED = "reused"          # files served from the SHA256 cache
STAT_FAILED = "failed"          # files that could not be parsed
STAT_SKIPPED = "skipped"        # files skipped (too big / excluded)
STAT_UNRESOLVED = "unresolved_calls"
STAT_BUILD_MS = "build_ms"

NODE_FILE = "file"
NODE_CLASS = "class"
NODE_FUNCTION = "function"
NODE_SYMBOL = "symbol"          # top-level consts (TS/JS variable_declarator)
# "module" is reserved for v2 knowledge-page nodes.

EDGE_IMPORTS = "imports"
EDGE_CALLS = "calls"
EDGE_INHERITS = "inherits"
# "contains" and "references" are schema-reserved for v2.


@dataclass
class GraphNode:
    id: str
    type: str
    name: str
    line: int = 0                # 1-based symbol start line; 0 for files
    lang: str | None = None     # file nodes only
    lines: int = 0              # file nodes only

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "type": self.type, "name": self.name}
        if self.line:
            data["line"] = self.line
        if self.lang:
            data["lang"] = self.lang
        if self.lines:
            data["lines"] = self.lines
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphNode":
        return cls(
            id=data["id"],
            type=data["type"],
            name=data["name"],
            line=data.get("line", 0),
            lang=data.get("lang"),
            lines=data.get("lines", 0),
        )


@dataclass
class GraphEdge:
    src: str
    dst: str
    type: str

    def to_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "type": self.type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphEdge":
        return cls(src=data["src"], dst=data["dst"], type=data["type"])


@dataclass
class RepoMeta:
    name: str
    head_sha: str
    langs: list[str]
    built_at: float


@dataclass
class RepoGraph:
    repo: RepoMeta
    stats: dict[str, int] = field(default_factory=dict)
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    # Lazy indexes (built once per instance on first adjacency()/node()).
    _by_id: dict[str, GraphNode] = field(default_factory=dict, repr=False)
    _fwd: dict[str, list[tuple[str, str]]] = field(default_factory=dict, repr=False)
    _rev: dict[str, list[tuple[str, str]]] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo": {
                "name": self.repo.name,
                "head_sha": self.repo.head_sha,
                "langs": self.repo.langs,
                "built_at": self.repo.built_at,
            },
            "stats": self.stats,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoGraph":
        repo = data["repo"]
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            repo=RepoMeta(
                name=repo["name"],
                head_sha=repo.get("head_sha", ""),
                langs=repo.get("langs", []),
                built_at=repo.get("built_at", 0.0),
            ),
            stats=data.get("stats", {}),
            nodes=[GraphNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[GraphEdge.from_dict(e) for e in data.get("edges", [])],
        )

    def node(self, node_id: str) -> GraphNode | None:
        if not self._by_id:
            self._by_id = {n.id: n for n in self.nodes}
        return self._by_id.get(node_id)

    def adjacency(self) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
        """Forward (src -> [(type, dst)]) and reverse (dst -> [(type, src)]) maps."""
        if not self._fwd and self.edges:
            for edge in self.edges:
                self._fwd.setdefault(edge.src, []).append((edge.type, edge.dst))
                self._rev.setdefault(edge.dst, []).append((edge.type, edge.src))
        return self._fwd, self._rev


# --- id helpers ---

def file_id(rel: str) -> str:
    """Node id for a repo-relative file path (forward slashes)."""
    return "f:" + rel.replace("\\", "/")


def symbol_id(rel: str, qname: str) -> str:
    """Node id for a symbol: ``s:{rel}:{qname}`` (qname is class-qualified)."""
    return f"s:{rel.replace('\\', '/')}:{qname}"


def parse_node_id(node_id: str) -> tuple[str, str, str | None]:
    """Split a node id into (kind, rel_path, qname|None)."""
    kind = node_id[:1]
    rest = node_id[2:]
    if kind == "f":
        return "file", rest, None
    rel, _, qname = rest.rpartition(":")
    return "symbol", rel, qname or None


def is_symbol_id(node_id: str) -> bool:
    return node_id.startswith("s:")


def repo_graph_dir(project_fs_path: str, repo_name: str) -> Path:
    """Storage dir for one repo's graph: ``{project}/.tmp/repo_graph/{name}``.

    Lives under .tmp so the checkout directory stays pristine; repo_name is
    validated by the caller against the same regex git_repo uses.
    """
    return Path(project_fs_path) / ".tmp" / "repo_graph" / repo_name


def now_epoch() -> float:
    return time.time()
