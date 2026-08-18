"""Knowledge service — bridges API layer with agent-core indexer and DB."""
from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("api.services.knowledge_service")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.knowledge import KnowledgeMetadata, KnowledgeVersion
from api.models.project import Project
from api.paths import PROJECTS_ROOT


async def get_project_fs_path(project_id: str, db: AsyncSession) -> str:
    """Resolve the filesystem path for a project workspace (always derived from config)."""
    from api.paths import resolve_project_fs_path
    return await resolve_project_fs_path(project_id, db)


async def reindex_knowledge(
    fs_path: str,
    project_id: str | None,
    db: AsyncSession,
) -> dict:
    """Re-index a single knowledge file."""
    try:
        from agent_core.knowledge.index import reindex_one
        return await reindex_one(
            fs_path=fs_path,
            project_id=project_id,
            db_session=db,
        )
    except Exception as e:
        _log.exception("reindex_knowledge failed for %s (project %s)", fs_path, project_id)
        return {"error": str(e)}


async def write_knowledge_version(
    knowledge_id: str,
    project_id: str,
    content: str,
    changed_by: str,
    change_summary: str,
    db: AsyncSession,
) -> None:
    """Save a version snapshot for a project-owned knowledge file."""
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.knowledge_id == knowledge_id,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    km = result.scalar_one_or_none()
    if not km:
        return

    next_version = (km.version or 0) + 1
    version = KnowledgeVersion(
        knowledge_id=knowledge_id,
        version_num=next_version,
        content=content,
        changed_by=changed_by,
        change_summary=change_summary,
    )
    db.add(version)
    km.version = next_version
    await db.flush()


async def get_completeness_by_category(
    project_id: str,
    db: AsyncSession,
) -> dict[str, float]:
    """Return {category: avg_completeness_score} for a project."""
    from sqlalchemy import func
    result = await db.execute(
        select(
            KnowledgeMetadata.category,
            func.round(func.avg(func.coalesce(KnowledgeMetadata.completeness_score, 0.0)), 1),
        ).where(
            (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        ).group_by(KnowledgeMetadata.category)
    )
    return {cat: float(avg) for cat, avg in result.all()}


async def list_knowledge_for_project(
    project_id: str,
    db: AsyncSession,
) -> list[dict]:
    """List all knowledge items for a project (project-scoped + global)."""
    result = await db.execute(
        select(KnowledgeMetadata).where(
            (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        ).order_by(KnowledgeMetadata.category, KnowledgeMetadata.slug)
    )
    out = []
    for k in result.scalars().all():
        out.append({
            "knowledge_id": str(k.knowledge_id),
            "slug": k.slug,
            "title": k.title,
            "category": k.category,
            "completeness_score": k.completeness_score,
            "tags": k.tags_list,
            "cross_references": k.cross_references_list,
            "word_count": k.word_count,
            "version": k.version,
            "fs_path": k.fs_path,
        })
    return out
