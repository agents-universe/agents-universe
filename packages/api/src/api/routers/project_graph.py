"""Project knowledge-graph endpoints for the 图谱 tab.

Thin HTTP layer over agent_core.knowledge.graph.project: authorize, resolve
the workspace, map KnowledgeMetadata rows to plain doc dicts, delegate. The
aggregator namespaces node ids per repo (``{repo}:{raw_id}`` / ``d:{slug}``);
this router never rewrites them.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.knowledge.graph.project import (
    DEFAULT_MAX_NODES,
    aggregate_project_graph,
    build_project_graphs,
    graph_edge_expansion,
    graph_node_neighbors,
)
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.errors import api_error
from api.models.knowledge import KnowledgeMetadata
from api.models.project import Project
from api.services.knowledge_service import get_project_fs_path

router = APIRouter(prefix="/api/projects/{project_id}")


class GraphBuildBody(BaseModel):
    force: bool = False


async def _doc_rows(db: AsyncSession, project_id: str) -> list[dict]:
    """Knowledge pages of THIS project (global framework docs excluded — the
    graph mirrors the project, and constant framework rows would clutter it)."""
    result = await db.execute(
        select(KnowledgeMetadata)
        .where(
            KnowledgeMetadata.project_id == project_id,
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        )
        .order_by(KnowledgeMetadata.slug)
    )
    return [
        {
            "slug": row.slug,
            "title": row.title,
            "summary": row.summary or "",
            "category": row.category,
            "cross_references": row.cross_references_list,
        }
        for row in result.scalars().all()
    ]


@router.get("/graph")
async def get_project_graph(
    project_id: str,
    level: str = "overview",
    max_nodes: int = DEFAULT_MAX_NODES,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    if level not in ("overview", "full"):
        raise api_error(400, "GRAPH_LEVEL_INVALID", f"Invalid level: {level!r}")
    fs_path = await get_project_fs_path(project_id, db)
    docs = await _doc_rows(db, project_id)
    # to_thread: loading a large graph.json (multi-MB JSON parse) on the event
    # loop would stall every WS heartbeat in the process — same reasoning as
    # builder._persist.
    return await asyncio.to_thread(
        aggregate_project_graph, fs_path, docs, level=level, max_nodes=max_nodes
    )


@router.get("/graph/neighbors")
async def get_graph_neighbors(
    project_id: str,
    node: str,
    depth: int = 1,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    repo, sep, raw_id = node.partition(":")
    # Doc nodes (d:{slug}) have no repo graph — their edges travel with the
    # main payload. A repo literally named "d" still works: its raw ids start
    # with f:/s: and are accepted below.
    if not sep or not repo or not raw_id or raw_id[:2] not in ("f:", "s:"):
        raise api_error(400, "GRAPH_NODE_INVALID",
                        f"node must be a code node id '{{repo}}:{{node_id}}': {node!r}")
    fs_path = await get_project_fs_path(project_id, db)
    result = await asyncio.to_thread(graph_node_neighbors, fs_path, repo, raw_id, depth)
    if result is None:
        raise api_error(404, "GRAPH_NODE_NOT_FOUND", f"Unknown graph node: {node!r}")
    return result


@router.get("/graph/edge")
async def get_graph_edge(
    project_id: str,
    src: str,
    dst: str,
    type: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Symbol-level edges rolled up into one overview edge (its drill-down).

    Both endpoints must be code node ids of the SAME repo (``{repo}:{f:|s:}``,
    same validation as neighbors); doc edges ship complete with the payload
    and never reach here.
    """
    src_repo, src_sep, src_raw = src.partition(":")
    dst_repo, dst_sep, dst_raw = dst.partition(":")
    valid = (
        src_sep and dst_sep and src_repo and src_repo == dst_repo
        and src_raw[:2] in ("f:", "s:") and dst_raw[:2] in ("f:", "s:")
    )
    if not valid:
        raise api_error(400, "GRAPH_EDGE_INVALID",
                        f"src/dst must be code node ids of one repo: {src!r}, {dst!r}")
    fs_path = await get_project_fs_path(project_id, db)
    result = await asyncio.to_thread(
        graph_edge_expansion, fs_path, src_repo, src_raw, dst_raw, type
    )
    if result is None:
        raise api_error(404, "GRAPH_EDGE_NOT_FOUND",
                        f"Unknown graph edge: {src!r} -[{type}]-> {dst!r}")
    return result


@router.post("/graph/build")
async def build_project_graph(
    project_id: str,
    body: GraphBuildBody = GraphBuildBody(),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    fs_path = await get_project_fs_path(project_id, db)
    # Synchronous per repo: the head_sha fast path makes no-op rebuilds
    # near-instant, oversized repos come back as status=skipped from the
    # builder's own guard, and each build already runs its heavy steps in
    # worker threads.
    repos = await build_project_graphs(fs_path, force=body.force)
    return {"repos": repos}
