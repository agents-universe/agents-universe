"""Knowledge management router."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import frontmatter as _fm
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.paths import KNOWLEDGE_SLUG_RE, PathEscapeError, is_within, resolve_within
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.knowledge import KnowledgeMetadata
from api.models.project import Project
from api.paths import FRAMEWORK_KNOWLEDGE_DIR, KNOWLEDGE_TEMPLATE_DIR
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


def _rehydrate_frontmatter(old_content: str, new_body: str) -> str:
    """Merge a body-only edit back into the file's existing frontmatter.

    The GET endpoint strips frontmatter before returning content, so a PUT
    carries only the body. Writing that body verbatim would silently drop the
    file's frontmatter (title/tags/parent/children) on save. Parse the
    on-disk frontmatter and emit body + preserved metadata; a file without
    frontmatter (or one that failed to parse) is written verbatim, matching
    what GET surfaced.
    """
    # A UTF-8 BOM hides the `---` block from python-frontmatter: metadata
    # would look absent and the merge would fall through to the body-only
    # write — wiping the frontmatter GET had stripped into that body.
    old_content = old_content.lstrip("﻿")
    try:
        post = _fm.loads(old_content)
    except Exception:
        return new_body
    if not post.metadata:
        return new_body
    return _fm.dumps(_fm.Post(new_body, **post.metadata))


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
        # 'index' is the legacy alias for 'root' — accept it so old clients
        # keep filtering correctly (indexer normalizes to 'root' on write).
        if level == "index":
            level = "root"
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
            # Lets the UI badge framework knowledge and hide its delete action:
            # global rows are read-only through every project-scoped path.
            "is_global": k.project_id is None,
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
    # Project rows only: completeness answers "how filled-in is THIS
    # project's knowledge", and the global framework docs carry constant
    # scores that would skew the system/technical averages of every project.
    # A category with no project rows simply drops out of the dict.
    result = await db.execute(
        select(
            KnowledgeMetadata.category,
            func.round(func.avg(func.coalesce(KnowledgeMetadata.completeness_score, 0.0)), 1),
        ).where(
            KnowledgeMetadata.project_id == project_id,
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


def _scan_templates() -> dict[str, dict]:
    """Map frontmatter slug -> {path, title, category, summary} for every
    system template file.

    Template files are FLAT under knowledge/_template/; their frontmatter
    ``slug`` (e.g. ``domain/context``) is the destination path inside a
    project — the same rule project creation applies (routers/projects.py).
    A missing/unparseable frontmatter falls back to the relative path, which
    for the flat layout equals the stem (mirrors
    project_categories._available_template_slugs tolerance).
    """
    out: dict[str, dict] = {}
    if not KNOWLEDGE_TEMPLATE_DIR.exists():
        return out
    for f in sorted(KNOWLEDGE_TEMPLATE_DIR.rglob("*.md")):
        rel = f.relative_to(KNOWLEDGE_TEMPLATE_DIR).with_suffix("").as_posix()
        try:
            raw = f.read_text("utf-8").lstrip("﻿")
            meta = _fm.loads(raw).metadata
        except Exception:
            _log.warning("Template %s: frontmatter parse failed", f.name, exc_info=True)
            meta = {}
        slug = str(meta.get("slug") or rel)
        if slug in out:
            _log.warning("Duplicate template slug %r (%s); keeping the first file", slug, f.name)
            continue
        out[slug] = {
            "path": f,
            "title": str(meta.get("title") or f.stem.replace("-", " ").title()),
            "category": str(meta.get("category") or slug.split("/")[0]),
            "summary": str(meta.get("summary") or ""),
        }
    return out


# Declared BEFORE the {slug:path} catch-all below: FastAPI matches literal
# routes in registration order (same rule as /knowledge/completeness), and a
# later registration would let the catch-all answer _templates with a 404
# from the disk fallback. The underscore prefix can never collide with a real
# knowledge slug either — KNOWLEDGE_SLUG_RE requires an alphanumeric first
# character — so a wrong order would 400 instead of serving some file.
@router.get("/knowledge/_templates")
async def list_knowledge_templates(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    project_fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(project_fs_path) / "knowledge"
    templates = await asyncio.to_thread(_scan_templates)
    return [
        {
            "slug": slug,
            "title": t["title"],
            "category": t["category"],
            "summary": t["summary"],
            "exists": (knowledge_dir / f"{slug}.md").exists(),
        }
        for slug, t in sorted(templates.items())
    ]


class TemplateCopy(BaseModel):
    slug: str


@router.post("/knowledge/_templates/copy")
async def copy_knowledge_template(
    project_id: str,
    body: TemplateCopy,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    if not _SLUG_RE.match(body.slug):
        raise HTTPException(status_code=400, detail=f"Invalid slug: {body.slug!r}")
    templates = await asyncio.to_thread(_scan_templates)
    entry = templates.get(body.slug)
    if entry is None:
        # Lookup is a dict key over scanned template slugs — no path is ever
        # built from the requested value, so traversal is structurally absent.
        raise HTTPException(status_code=404, detail=f"Template not found: {body.slug!r}")

    project_fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(project_fs_path) / "knowledge"
    dest = _resolve_slug_file(knowledge_dir, body.slug)
    await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)

    src: Path = entry["path"]
    created = True
    try:
        data = await asyncio.to_thread(src.read_bytes)

        def _write_exclusive() -> None:
            # Exclusive create: a concurrent copy must never clobber the
            # winner's file or the user's edits to it — the same
            # never-overwrite contract as project creation.
            with open(dest, "xb") as fh:
                fh.write(data)

        await asyncio.to_thread(_write_exclusive)
    except FileExistsError:
        created = False

    if created:
        # Inline reindex: this inserts one new row, so unlike the PUT path
        # (which uses a background task to dodge its own row locks) there is
        # nothing to deadlock against.
        result = await reindex_knowledge(str(dest), project_id, db)
        if not result or "error" in result:
            # The file is on disk and the list falls back to disk reads —
            # same visibility stance as knowledge_rw: written-but-unindexed
            # is surfaced, not hidden.
            _log.warning("Template copy reindex failed for %s: %s", body.slug, result)
        else:
            # Score parity with project creation (routers/projects.py): a
            # freshly copied, unfilled scaffold shows 0%, not the indexer's
            # structural score.
            await db.execute(
                update(KnowledgeMetadata)
                .where(
                    KnowledgeMetadata.project_id == project_id,
                    KnowledgeMetadata.slug == body.slug,
                )
                .values(completeness_score=0, coverage_breadth=0, recency_score=0)
            )
            await db.commit()

    cache = getattr(request.app.state, "knowledge_cache", None)
    if cache is not None:
        cache.invalidate(project_id)
    return {"slug": body.slug, "created": created}


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
            # Strip the BOM so the `---` block parses — otherwise GET hands
            # the whole file (frontmatter included) to the editor as content.
            raw = (await asyncio.to_thread(fs_path.read_text, "utf-8")).lstrip("﻿")
            try:
                post = _fm.loads(raw)
                # Fall back to raw ONLY when nothing parsed as frontmatter:
                # an FM file with an empty body would otherwise send its own
                # frontmatter to the editor as "body", and rehydrating that
                # nests a second copy of the frontmatter on save.
                content = post.content if post.content.strip() or post.metadata else raw
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
            # Same flag as the list endpoint — the viewer needs it to badge
            # framework knowledge and hide its (403-ing) delete action.
            "is_global": km.project_id is None,
        }

    # Not in DB — try reading primary file from disk
    file_path = _resolve_slug_file(knowledge_dir, slug)

    if not await asyncio.to_thread(file_path.exists):
        raise HTTPException(status_code=404, detail=f"Knowledge file not found: {slug}")

    stat = await asyncio.to_thread(file_path.stat)
    if stat.st_size > _MAX_READ_SIZE:
        raise HTTPException(status_code=413, detail="Knowledge file too large to read")

    # Strip the BOM so the `---` block parses — otherwise GET hands the
    # whole file (frontmatter included) to the editor as content.
    raw = (await asyncio.to_thread(file_path.read_text, "utf-8")).lstrip("﻿")
    try:
        post = _fm.loads(raw)
        meta = post.metadata
        # Fall back to raw ONLY when nothing parsed as frontmatter: an FM
        # file with an empty body would otherwise send its own frontmatter
        # to the editor as "body", and rehydrating that nests a second copy
        # of the frontmatter on save.
        content = post.content if post.content.strip() or post.metadata else raw
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
        # A disk-fallback file is by construction a project's own file —
        # global knowledge always has a DB row backing it.
        "is_global": False,
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

        # GET strips frontmatter from the content it returns, so the PUT body
        # is body-only: re-merge it with the file's existing frontmatter so a
        # save never wipes title/tags/parent metadata.
        write_content = _rehydrate_frontmatter(old_content, body.content)
        try:
            await asyncio.to_thread(fs_path.write_text, write_content, "utf-8")
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
        # Commit the version snapshot NOW, before the reindex background task
        # runs: Starlette runs background tasks during response send, which
        # precedes get_db's dependency-teardown commit. If this tx stayed open,
        # the reindex's UPDATE on the same knowledge_metadata row would block
        # on our row locks while we wait for the response (deadlock). Same
        # contract as workspace_files' version write.
        await db.commit()

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

    # Same contract as the DB branch: the GET stripped frontmatter, so merge
    # the body-only edit back into the on-disk frontmatter.
    old_content = await asyncio.to_thread(file_path.read_text, "utf-8")
    write_content = _rehydrate_frontmatter(old_content, body.content)
    try:
        await asyncio.to_thread(file_path.write_text, write_content, "utf-8")
    except OSError:
        raise HTTPException(status_code=507, detail="Failed to write knowledge file")

    return {"slug": slug, "updated": True}


@router.delete("/knowledge/{slug:path}")
async def delete_knowledge(
    project_id: str,
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Delete a project's own knowledge file and its metadata row.

    Mirrors agent_core.tools.knowledge_rw._op_delete (the agent's delete
    path): read the parent link before unlinking, sync the parent's
    children list, then best-effort row deletion — file removal is the
    source-of-truth change and never blocks on DB bookkeeping.

    Global (framework) knowledge is read-only through every project-scoped
    path: it has no project row and no project file, so it fails the
    ownership gate below with 403 rather than 404 (404 would be a lie — the
    slug does exist, just not as this project's to remove).
    """
    project_fs_path = await get_project_fs_path(project_id, db)
    knowledge_dir = Path(project_fs_path) / "knowledge"
    file_path = _resolve_slug_file(knowledge_dir, slug)  # 400 on bad slug

    result = await db.execute(
        select(KnowledgeMetadata).where(
            KnowledgeMetadata.slug == slug,
            KnowledgeMetadata.project_id == project_id,
        )
    )
    project_km = result.scalar_one_or_none()
    file_exists = await asyncio.to_thread(file_path.exists)

    if project_km is None and not file_exists:
        result = await db.execute(
            select(KnowledgeMetadata.knowledge_id).where(
                KnowledgeMetadata.slug == slug,
                KnowledgeMetadata.project_id == None,  # noqa: E711
            ).limit(1)
        )
        if result.first() is not None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "global_knowledge",
                    "message": "Framework knowledge is read-only",
                },
            )
        raise HTTPException(status_code=404, detail=f"Knowledge not found: {slug}")

    # Read the parent BEFORE unlinking so the parent's children list can be
    # cleaned up symmetrically with the write-side sync.
    parent_slug: str | None = None
    if file_exists:
        try:
            raw = await asyncio.to_thread(file_path.read_text, "utf-8")
            parent_slug = str(_fm.loads(raw.lstrip("﻿")).metadata.get("parent") or "") or None
        except Exception:
            parent_slug = None

        try:
            await asyncio.to_thread(file_path.unlink)
        except OSError:
            raise HTTPException(status_code=507, detail="Failed to delete knowledge file")

    # Symmetric hierarchy cleanup: drop this slug from the parent's
    # children list (best-effort, never blocks the delete).
    if parent_slug:
        try:
            from agent_core.knowledge.index import sync_parent_children

            await sync_parent_children(
                child_slug=slug,
                parent_slug=parent_slug,
                project_id=project_id,
                db_session=db,
                knowledge_dir=knowledge_dir,
                add=False,
            )
        except Exception:
            _log.warning(
                "Parent children cleanup failed: child=%s parent=%s project=%s",
                slug, parent_slug, project_id, exc_info=True,
            )

    # Best-effort row deletion (delete_one matches exact project_id, so a
    # global row of the same slug — the shadowing case — is never touched).
    db_action = "skipped"
    try:
        from agent_core.knowledge.index import delete_one

        db_result = await delete_one(slug, project_id, db)
        db_action = db_result.get("action", "error")
    except Exception:
        _log.warning(
            "Knowledge row delete failed for slug=%s project=%s",
            slug, project_id, exc_info=True,
        )

    cache = getattr(request.app.state, "knowledge_cache", None)
    if cache is not None:
        cache.invalidate(project_id)

    return {"deleted": True, "slug": slug, "file_deleted": file_exists, "db_row": db_action}


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
