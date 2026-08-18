"""Knowledge management router."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import frontmatter as _fm
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.paths import KNOWLEDGE_SLUG_RE, PathEscapeError, is_within, resolve_within
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.knowledge import KnowledgeMetadata
from api.models.project import Project
from api.paths import FRAMEWORK_KNOWLEDGE_DIR
from api.services.knowledge_service import (
    get_project_fs_path,
    reindex_knowledge,
    write_knowledge_version,
)

router = APIRouter(prefix="/api/projects/{project_id}")

_log = logging.getLogger(__name__)

_CROSS_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")

# Shared with agent_core.tools.knowledge_rw via agent_core.paths. Allows
# uppercase/dots (legacy filenames are indexed verbatim) but rejects "."
# and ".." segments and backslashes, blocking traversal attempts.
_SLUG_RE = KNOWLEDGE_SLUG_RE


def _resolve_slug_file(knowledge_dir: Path, slug: str) -> Path:
    """Validate a knowledge slug and resolve it to a file inside knowledge_dir."""
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail=f"Invalid slug: {slug!r}")
    try:
        return resolve_within(knowledge_dir, f"{slug}.md")
    except PathEscapeError:
        raise HTTPException(status_code=400, detail=f"Invalid slug: {slug!r}")


def _checked_db_fs_path(km: KnowledgeMetadata, project_knowledge_dir: Path) -> Path | None:
    """Return km.fs_path as a Path after verifying directory ownership.

    Global rows (project_id NULL) must live under the framework knowledge
    directory; project rows under the project's own knowledge/ directory.
    Raises 400 on any violation — a DB row pointing elsewhere is treated as
    tampered data, never as a read/write target.
    """
    if not km.fs_path:
        return None
    if km.project_id is None:
        allowed = is_within(FRAMEWORK_KNOWLEDGE_DIR, km.fs_path)
    else:
        allowed = is_within(project_knowledge_dir, km.fs_path)
    if not allowed:
        _log.warning(
            "Refusing knowledge fs_path outside its owning directory: slug=%s project_id=%s",
            km.slug, km.project_id,
        )
        raise HTTPException(status_code=400, detail="Invalid knowledge file location")
    return Path(km.fs_path)


@router.get("/knowledge")
async def list_knowledge(
    project_id: str,
    limit: int = 200,
    offset: int = 0,
    level: str | None = None,
    root_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    query = select(KnowledgeMetadata).where(
        (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
        KnowledgeMetadata.is_archived == False,  # noqa: E712
    )
    if level:
        query = query.where(KnowledgeMetadata.knowledge_level == level)
    if root_only:
        query = query.where(KnowledgeMetadata.parent_slug == None)  # noqa: E711
    query = query.order_by(KnowledgeMetadata.category, KnowledgeMetadata.slug).offset(offset).limit(limit)
    result = await db.execute(query)
    items = result.scalars().all()
    return [
        {
            "knowledge_id": str(k.knowledge_id),
            "slug": k.slug,
            "title": k.title,
            "category": k.category,
            "completeness_score": k.completeness_score,
            "tags": k.tags_list,
            "word_count": k.word_count,
            "knowledge_level": k.knowledge_level or "auto",
            "parent_slug": k.parent_slug,
            "children_slugs": k.children_list,
            "summary": k.summary or "",
            "depth": k.depth,
        }
        for k in items
    ]


@router.get("/knowledge/completeness")
async def knowledge_completeness(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
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


@router.get("/knowledge/{slug:path}/children")
async def get_knowledge_children(
    project_id: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.parent_slug == slug,
            (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
            KnowledgeMetadata.is_archived == False,  # noqa: E712
        ).order_by(KnowledgeMetadata.slug)
    )
    children = result.scalars().all()
    return [
        {
            "slug": c.slug,
            "title": c.title,
            "summary": c.summary or "",
            "has_children": bool(c.children_list),
            "depth": c.depth,
        }
        for c in children
    ]


@router.get("/knowledge/{slug:path}/ancestors")
async def get_knowledge_ancestors(
    project_id: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    ancestors: list[dict] = []
    current_slug = slug
    max_depth = 5

    for _ in range(max_depth):
        result = await db.execute(
            select(KnowledgeMetadata.parent_slug, KnowledgeMetadata.title).where(
                KnowledgeMetadata.slug == current_slug,
                (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
            )
            # Same slug may exist globally and project-scoped — project row
            # wins, and the limit keeps one_or_none() from raising when both
            # rows exist (MultipleResultsFound would 500 the endpoint).
            # CASE instead of a bare predicate: T-SQL rejects ORDER BY on a
            # boolean expression (ORDER BY project_id IS NOT NULL is a syntax
            # error on SQL Server), so NULL rows sort last via 1/0.
            .order_by(case((KnowledgeMetadata.project_id.is_(None), 1), else_=0))
            .limit(1)
        )
        row = result.one_or_none()
        if not row or not row[0]:
            break
        parent_slug = row[0]
        parent_result = await db.execute(
            select(KnowledgeMetadata.slug, KnowledgeMetadata.title).where(
                KnowledgeMetadata.slug == parent_slug,
                (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
            )
            .order_by(case((KnowledgeMetadata.project_id.is_(None), 1), else_=0))
            .limit(1)
        )
        parent_row = parent_result.one_or_none()
        if not parent_row:
            break
        ancestors.insert(0, {"slug": parent_row[0], "title": parent_row[1]})
        current_slug = parent_slug

    return ancestors


@router.get("/knowledge/{slug:path}/versions")
async def get_knowledge_versions(
    project_id: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    from api.models.knowledge import KnowledgeVersion
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    km = result.scalar_one_or_none()
    if not km:
        raise HTTPException(status_code=404, detail=f"Not found: {slug}")

    result = await db.execute(
        select(KnowledgeVersion)
        .where(KnowledgeVersion.knowledge_id == km.knowledge_id)
        .order_by(KnowledgeVersion.version_num.desc())
        .limit(20)
    )
    versions = result.scalars().all()
    return [
        {
            "version_num": v.version_num,
            "changed_by": v.changed_by,
            "change_summary": v.change_summary,
            "created_at": v.created_at.isoformat(),
        }
        for v in versions
    ]


@router.get("/knowledge/{slug:path}")
async def get_knowledge(
    project_id: str,
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    _MAX_READ_SIZE = 10 * 1024 * 1024  # 10 MB

    project_fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(project_fs_path) / "knowledge"

    # First try DB (detail files)
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            (KnowledgeMetadata.project_id == project_id) | (KnowledgeMetadata.project_id == None),  # noqa: E711
        )
        # The same slug can exist as both a global row (project_id NULL) and
        # a project row — the project-specific one wins. Without the ORDER BY,
        # .first() would be nondeterministic; without .limit(1) the scalar
        # variants raise MultipleResultsFound → 500. CASE (not a boolean
        # predicate) — T-SQL rejects boolean ORDER BY expressions.
        .order_by(case((KnowledgeMetadata.project_id.is_(None), 1), else_=0))
        .limit(1)
    )
    km = result.scalar_one_or_none()

    if km:
        content = ""
        fs_path = _checked_db_fs_path(km, knowledge_dir)
        if fs_path and await asyncio.to_thread(fs_path.exists):
            stat = await asyncio.to_thread(fs_path.stat)
            if stat.st_size > _MAX_READ_SIZE:
                raise HTTPException(status_code=413, detail="Knowledge file too large to read")
            raw = await asyncio.to_thread(fs_path.read_text, "utf-8")
            try:
                post = _fm.loads(raw)
                content = post.content if post.content.strip() else raw
            except Exception:
                content = raw
        return {
            "knowledge_id": str(km.knowledge_id),
            "slug": km.slug,
            "title": km.title,
            "category": km.category,
            "content": content,
            "completeness_score": km.completeness_score,
            "tags": km.tags_list,
            "cross_references": km.cross_references_list,
            "version": km.version,
            "parent_slug": km.parent_slug,
            "children_slugs": km.children_list,
            "depth": km.depth,
        }

    # Not in DB — try reading primary file from disk
    file_path = _resolve_slug_file(knowledge_dir, slug)

    if not await asyncio.to_thread(file_path.exists):
        raise HTTPException(status_code=404, detail=f"Knowledge file not found: {slug}")

    stat = await asyncio.to_thread(file_path.stat)
    if stat.st_size > _MAX_READ_SIZE:
        raise HTTPException(status_code=413, detail="Knowledge file too large to read")

    raw = await asyncio.to_thread(file_path.read_text, "utf-8")
    try:
        post = _fm.loads(raw)
        meta = post.metadata
        content = post.content if post.content.strip() else raw
    except Exception:
        meta = {}
        content = raw

    title = meta.get("title") or slug.split("/")[-1].replace("-", " ").title()
    category = meta.get("category") or slug.split("/")[0]
    tags = meta.get("tags", [])
    cross_refs = _CROSS_REF_RE.findall(content) if content else []
    children = meta.get("children", [])

    return {
        "knowledge_id": f"disk:{slug}",
        "slug": slug,
        "title": title,
        "category": category,
        "content": content,
        "completeness_score": 0.0,
        "tags": tags if isinstance(tags, list) else [],
        "cross_references": cross_refs,
        "version": 1,
        "parent_slug": meta.get("parent"),
        "children_slugs": children if isinstance(children, list) else [],
        "depth": 0,
    }


_MAX_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB


class KnowledgeWrite(BaseModel):
    content: str
    change_summary: str = "Updated"


@router.put("/knowledge/{slug:path}")
async def update_knowledge(
    project_id: str,
    slug: str,
    body: KnowledgeWrite,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    if len(body.content.encode("utf-8")) > _MAX_WRITE_SIZE:
        raise HTTPException(status_code=413, detail="Content too large")

    project_fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(project_fs_path) / "knowledge"

    # Try DB first (detail files)
    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    km = result.scalar_one_or_none()

    if km and km.fs_path:
        # Project-owned rows only (query above): writes must stay inside the
        # project's own knowledge/ directory. Global knowledge is never
        # writable through a project-scoped API.
        fs_path = _checked_db_fs_path(km, knowledge_dir)
        old_content = ""
        if await asyncio.to_thread(fs_path.exists):
            old_content = await asyncio.to_thread(fs_path.read_text, "utf-8")

        try:
            await asyncio.to_thread(fs_path.write_text, body.content, "utf-8")
        except OSError:
            raise HTTPException(status_code=507, detail="Failed to write knowledge file")

        await write_knowledge_version(
            knowledge_id=str(km.knowledge_id),
            project_id=project_id,
            content=old_content,
            changed_by="user",
            change_summary=body.change_summary,
            db=db,
        )

        # Process-level knowledge cache must be evicted after the reindex
        # lands, or the next conversation keeps serving stale entries.
        cache = getattr(request.app.state, "knowledge_cache", None)

        async def _reindex_in_new_session():
            from api.database import AsyncSessionLocal
            async with AsyncSessionLocal() as new_db:
                await reindex_knowledge(km.fs_path, project_id, new_db)
            if cache is not None:
                cache.invalidate(project_id)

        background_tasks.add_task(_reindex_in_new_session)
        return {"slug": slug, "updated": True}

    # Primary file — write directly to disk (confined to this project)
    file_path = _resolve_slug_file(knowledge_dir, slug)

    if not await asyncio.to_thread(file_path.exists):
        raise HTTPException(status_code=404, detail=f"Knowledge file not found: {slug}")

    try:
        await asyncio.to_thread(file_path.write_text, body.content, "utf-8")
    except OSError:
        raise HTTPException(status_code=507, detail="Failed to write knowledge file")

    return {"slug": slug, "updated": True}


@router.post("/knowledge/reindex")
async def trigger_reindex(
    project_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    from agent_core.knowledge.index import index_directory

    fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(fs_path) / "knowledge"

    cache = getattr(request.app.state, "knowledge_cache", None)

    async def _run():
        from api.database import AsyncSessionLocal
        async with AsyncSessionLocal() as new_db:
            await index_directory(knowledge_dir, project_id=project_id, db_session=new_db)
        if cache is not None:
            cache.invalidate(project_id)

    background_tasks.add_task(_run)
    return {"status": "reindex_started", "project_id": project_id}
