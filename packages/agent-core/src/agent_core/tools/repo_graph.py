"""Code knowledge-graph tool for cloned git repositories.

A deterministic tree-sitter graph (nodes + edges, no LLM) lives per checkout
under ``.tmp/repo_graph/<repo>/``, built automatically on git_repo
clone/checkout/pull. This tool answers structural questions — where a symbol
is, what calls it, what would break — in a few tokens instead of reading
whole files. The graph is a map, not a spec: dynamic references may be
missed, so read files for semantics.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..knowledge.graph.builder import build_repo_graph
from ..knowledge.graph import queries
from ..knowledge.graph.model import repo_graph_dir
from ..knowledge.graph.report import REPORT_FILE, compact_map
from ..knowledge.graph.store import GRAPH_FILE, load_cached
from ._repo_paths import list_clones, resolve_repo_path
from .base import Tool, ToolContext

_log = logging.getLogger("agent_core.repo_graph")


class RepoGraphTool(Tool):
    name = "repo_graph"
    prompt_hint = (
        "Query the code knowledge graph of cloned git repositories (auto-built on "
        "clone/checkout/pull). Consult it BEFORE reading files: query/neighbors/"
        "impact/path answer structural questions in a few tokens. The graph is "
        "deterministic and may miss dynamic references; read files for semantics."
    )
    description = (
        "Code knowledge graph for git clones. Operations: build, query, neighbors, "
        "impact, path, report. Requires exactly one of 'repository' or "
        "'repository_path'. build (re)creates the graph for a checkout; query "
        "searches symbols/files; neighbors lists symbols connected to one; impact "
        "lists what depends on a symbol (refactor blast radius); path finds the "
        "shortest call path between two symbols; report returns the compact repo map."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["build", "query", "neighbors", "impact", "path", "report"],
            },
            "repository": {
                "type": "string",
                "description": (
                    "Clone name (or owner/repo); exclusive with repository_path. "
                    "Required for every operation."
                ),
            },
            "repository_path": {
                "type": "string",
                "description": (
                    "Project-relative existing checkout; exclusive with repository. "
                    "Required for every operation."
                ),
            },
            "query": {
                "type": "string",
                "description": "Symbol/file name (substring) for query; symbol name for others.",
            },
            "symbol": {"type": "string", "description": "Symbol name for neighbors/impact."},
            "from": {"type": "string", "description": "Path start (symbol or file)."},
            "to": {"type": "string", "description": "Path end (symbol or file)."},
            "depth": {"type": "integer", "description": "Neighbors depth, 1-2 (default 1)."},
            "max_hops": {"type": "integer", "description": "Path search limit, 1-6 (default 6)."},
            "force": {
                "type": "boolean",
                "description": "Rebuild even when nothing changed (build only; default true).",
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params.get("operation")
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            return {"error": f"Unknown operation: {operation!r}"}
        return await handler(params, context)

    # --- repo resolution -------------------------------------------------

    def _require_repo(
        self, params: dict[str, Any], context: ToolContext
    ) -> tuple[Path | None, dict[str, Any] | None]:
        path, error = resolve_repo_path(
            params, context.project_fs_path,
            available=list_clones(context.project_fs_path),
        )
        if error:
            return None, error
        if not path.is_dir() or not (path / ".git").exists():
            return None, {
                "error": f"Repository not found at {self._display(path, context)}. Clone it first."
            }
        return path, None

    @staticmethod
    def _display(path: Path, context: ToolContext) -> str:
        relative = path.resolve().relative_to(Path(context.project_fs_path).resolve()).as_posix()
        return relative or "."

    @staticmethod
    def _no_graph_hint(repo: Path, context: ToolContext) -> dict[str, Any]:
        kg_dir = repo_graph_dir(context.project_fs_path, repo.name)
        return {
            "error": f"No graph for {repo.name!r} yet",
            "hint": "Run repo_graph build (clone/checkout/pull via git_repo auto-builds).",
            "graph_path": str(kg_dir),
        }

    # --- operations -------------------------------------------------------

    async def _op_build(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        # An explicit build means "rebuild after edits" — force by default so
        # the fast path can't mask a dirty working tree.
        force = bool(params.get("force", True))
        kg_dir = repo_graph_dir(context.project_fs_path, repo.name)
        try:
            return await build_repo_graph(repo, kg_dir, force=force)
        except Exception as exc:  # never fail the turn on a graph build
            _log.exception("repo_graph build failed for %s", repo.name)
            return {"error": f"Build failed: {exc}"}

    async def _op_query(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        graph = load_cached(repo_graph_dir(context.project_fs_path, repo.name))
        if graph is None:
            return self._no_graph_hint(repo, context)
        query = params.get("query", "")
        if not query:
            return {"error": "Parameter 'query' is required for query"}
        return queries.search(graph, query)

    async def _op_neighbors(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        graph = load_cached(repo_graph_dir(context.project_fs_path, repo.name))
        if graph is None:
            return self._no_graph_hint(repo, context)
        symbol = params.get("symbol") or params.get("query") or ""
        if not symbol:
            return {"error": "Parameter 'symbol' is required for neighbors"}
        node = queries.resolve_node(graph, symbol)
        if "error" in node:
            return node
        try:
            depth = int(params.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        result = queries.neighbors(graph, node["node_id"], depth=depth)
        result["node"] = node
        return result

    async def _op_impact(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        graph = load_cached(repo_graph_dir(context.project_fs_path, repo.name))
        if graph is None:
            return self._no_graph_hint(repo, context)
        symbol = params.get("symbol") or params.get("query") or ""
        if not symbol:
            return {"error": "Parameter 'symbol' is required for impact"}
        node = queries.resolve_node(graph, symbol)
        if "error" in node:
            return node
        result = queries.impact_set(graph, node["node_id"])
        result["node"] = node
        return result

    async def _op_path(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        graph = load_cached(repo_graph_dir(context.project_fs_path, repo.name))
        if graph is None:
            return self._no_graph_hint(repo, context)
        start = params.get("from") or params.get("query") or ""
        end = params.get("to") or params.get("symbol") or ""
        if not start or not end:
            return {"error": "Parameters 'from' and 'to' are required for path"}
        try:
            max_hops = int(params.get("max_hops", queries.MAX_HOPS))
        except (TypeError, ValueError):
            max_hops = queries.MAX_HOPS
        result = queries.shortest_path(graph, start, end, max_hops=max_hops)
        if "path" in result:
            result["path"] = [
                {"node_id": node_id, "name": _node_name(graph, node_id)}
                for node_id in result["path"]
            ]
        return result

    async def _op_report(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        repo, error = self._require_repo(params, context)
        if error:
            return error
        kg_dir = repo_graph_dir(context.project_fs_path, repo.name)
        graph = load_cached(kg_dir)
        if graph is None:
            return self._no_graph_hint(repo, context)
        # Only the compact map enters context; the full report stays on disk.
        return {
            "repo_map": compact_map(graph),
            "graph_path": str(kg_dir / GRAPH_FILE),
            "report_path": str(kg_dir / REPORT_FILE),
        }


def _node_name(graph, node_id: str) -> str:
    node = graph.node(node_id)
    return node.name if node else node_id
