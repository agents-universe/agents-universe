"""Project management router."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

_log = logging.getLogger("agents_universe.projects")
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.project import Project
from api.models.project_member import ProjectMember
from api.paths import KNOWLEDGE_TEMPLATE_DIR, PROJECTS_ROOT, PACKAGE_ROOT
from api.project_categories import DEFAULT_CATEGORY, get_categories, get_category, get_template_slugs

router = APIRouter(prefix="/api/projects")


def _to_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower().strip()).strip("-")[:50] or "project"


class ProjectCreateBody(BaseModel):
    # display_name column is Unicode(255) — MSSQL DataError (500) on
    # overflow .
    display_name: str = Field(max_length=255)
    category: str = Field(default=DEFAULT_CATEGORY, max_length=100)


class ProjectDeleteBody(BaseModel):
    confirmation: str


class ProjectPatchBody(BaseModel):
    visibility: Literal["public", "private"]


async def _member_project_ids(
    db: AsyncSession, user_id: str, project_ids: list[str]
) -> set[str]:
    """Project ids this user is whitelisted on (single query, no N+1)."""
    if not project_ids:
        return set()
    rows = await db.execute(
        select(ProjectMember.project_id).where(
            ProjectMember.user_id == user_id,
            ProjectMember.project_id.in_(project_ids),
        )
    )
    return {str(pid) for pid in rows.scalars().all()}


def _error(exc) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={
            "code": exc.code,
            "message": exc.message,
            "deletion_id": getattr(exc, "deletion_id", None),
            "retryable": getattr(exc, "retryable", False),
        },
    )


def _project_fs_path(project: Project, parent_slug: str | None = None) -> Path:
    """Workspace path for a project — mirrors paths.resolve_project_fs_path.

    listings returned ``PROJECTS_ROOT / slug`` unconditionally, so
    child projects (workspace at ``PROJECTS_ROOT/{parent.slug}/projects/
    {child.slug}``) reported a nonexistent root-level path.
    """
    if project.parent_id and parent_slug:
        return PROJECTS_ROOT / parent_slug / "projects" / project.slug
    return PROJECTS_ROOT / project.slug


def _serialize_project(
    project: Project,
    user_id: str,
    child_ids: set[str] | None = None,
    parent_slug: str | None = None,
    member_project_ids: set[str] | None = None,
) -> dict:
    # child_ids is precomputed once for the whole listing instead of
    # one subquery per project (N+1).
    category = project.category or DEFAULT_CATEGORY
    is_owner = project.created_by is not None and project.created_by == user_id
    return {
        "project_id": str(project.project_id),
        "slug": project.slug,
        "display_name": project.display_name,
        "description": project.description,
        "category": category,
        "category_label": (get_category(category) or {}).get("label", category),
        "parent_id": str(project.parent_id) if project.parent_id else None,
        "can_delete": is_owner
            and (child_ids is None or project.project_id not in child_ids),
        "fs_path": str(_project_fs_path(project, parent_slug)),
        "created_by": project.created_by,
        "visibility": project.visibility,
        "is_owner": is_owner,
        "can_manage": is_owner or (
            member_project_ids is not None
            and project.project_id in member_project_ids
        ),
    }


@router.post("")
async def create_project(
    body: ProjectCreateBody,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Create a new project with initialized knowledge from templates."""
    # 0. Validate category before any DB/filesystem writes
    category = body.category or DEFAULT_CATEGORY
    if get_category(category) is None:
        valid = [c["slug"] for c in get_categories()]
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unknown_category",
                "message": f"未知项目分类: '{category}',可选: {', '.join(valid)}",
                "valid_categories": valid,
            },
        )

    # 1. Generate unique slug
    base_slug = _to_slug(body.display_name)
    slug = base_slug
    suffix = 1
    while True:
        existing = await db.execute(
            select(Project).where(Project.slug == slug)
        )
        if not existing.scalar_one_or_none():
            break
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    # 2. Compute filesystem path
    fs_path = PROJECTS_ROOT / slug
    # A workspace directory with no DB row (manual creation, leftover from a
    # failed deletion) must not be silently adopted: template copy2 would
    # overwrite same-named files inside it and the new project would manage a
    # workspace it never created. Refuse the slug before the DB insert.
    if fs_path.exists() or fs_path.is_symlink():
        raise HTTPException(
            status_code=409,
            detail={"code": "slug_conflict", "message": "工作区路径已被占用，请更换项目名称"},
        )

    # 3. Insert project record — commit DB first to release locks quickly.
    # The slug check above is check-then-insert: a concurrent request can win
    # between the SELECT and the INSERT, so retry with an incremented suffix
    # on IntegrityError instead of surfacing a 500 to the client.
    for attempt in range(10):
        project = Project(
            slug=slug,
            display_name=body.display_name,
            category=category,
            created_by=current_user.user_id,
        )
        db.add(project)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
            suffix += 1
            slug = f"{base_slug}-{suffix}"
            fs_path = PROJECTS_ROOT / slug
            # The pre-insert existence check ran for the FIRST slug only — a
            # retried slug may sit on a leftover (DB-less) directory from a
            # failed creation/deletion and must not be silently adopted.
            if fs_path.exists() or fs_path.is_symlink():
                raise HTTPException(
                    status_code=409,
                    detail={"code": "slug_conflict", "message": "工作区路径已被占用，请更换项目名称"},
                )
    else:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "slug_conflict",
                "message": "并发创建项目冲突，请重试",
            },
        )
    await db.refresh(project)

    # 4. Create directories and copy templates AFTER commit (no lock held)
    def _init_filesystem() -> Path:
        knowledge_dir = fs_path / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        tests_dir = fs_path / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (fs_path / ".tmp" / "media").mkdir(parents=True, exist_ok=True)
        (fs_path / ".tmp" / "work").mkdir(parents=True, exist_ok=True)
        # Project-scoped agent definitions, skills, and workflows
        (fs_path / "agents").mkdir(parents=True, exist_ok=True)
        (fs_path / "skills").mkdir(parents=True, exist_ok=True)
        (fs_path / "workflows").mkdir(parents=True, exist_ok=True)

        # Copy test scaffold (package.json, playwright.config.ts, tsconfig.json)
        scaffold_dir = PACKAGE_ROOT / "scaffold" / "tests"
        if scaffold_dir.exists():
            for f in scaffold_dir.iterdir():
                if f.is_file():
                    dest = tests_dir / f.name
                    if not dest.exists():
                        shutil.copy2(f, dest)

        if KNOWLEDGE_TEMPLATE_DIR.exists():
            import frontmatter
            allowed = get_template_slugs(category)  # None = 全量拷贝(software 默认)
            for template_file in KNOWLEDGE_TEMPLATE_DIR.rglob("*.md"):
                post = frontmatter.loads(template_file.read_text("utf-8"))
                slug_val = post.metadata.get("slug")
                if allowed is not None:
                    if slug_val:
                        if slug_val not in allowed:
                            continue
                    elif not any(
                        t == template_file.stem or t.endswith(f"/{template_file.stem}")
                        for t in allowed
                    ):
                        continue  # 无 slug 时按文件名 stem 容错匹配
                if slug_val:
                    dest = knowledge_dir / f"{slug_val}.md"
                else:
                    rel = template_file.relative_to(KNOWLEDGE_TEMPLATE_DIR)
                    dest = knowledge_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    continue  # never overwrite an existing workspace file
                shutil.copy2(template_file, dest)
        return knowledge_dir

    # filesystem init failing after the DB commit (disk full,
    # permission error, PROJECTS_ROOT/slug occupied by a file) left a zombie
    # project: visible in listings but unusable — resolve_project_fs_path
    # fails and it can never be created again. Deactivate the row so it
    # disappears from listings and authorize_project rejects it.
    try:
        knowledge_dir = await asyncio.to_thread(_init_filesystem)
    except Exception:
        _log.error("Filesystem init failed for project %s; deactivating row", slug, exc_info=True)
        project.is_active = False
        await db.commit()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "filesystem_init_failed",
                "message": "项目工作区创建失败（磁盘或权限问题），请检查服务端环境后重试",
            },
        )

    # 5. Index knowledge files, then reset scores to 0 for fresh templates
    project_id = str(project.project_id)
    try:
        from agent_core.knowledge.index import index_directory
        from api.database import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as index_db:
            await index_directory(knowledge_dir, project_id, index_db)
            await index_db.execute(
                text("UPDATE knowledge_metadata SET completeness_score = 0, coverage_breadth = 0, recency_score = 0 WHERE project_id = :pid"),
                {"pid": project_id},
            )
            await index_db.commit()
    except Exception:
        _log.warning("Knowledge auto-index failed for project %s", project_id, exc_info=True)

    return _serialize_project(project, current_user.user_id)


@router.get("")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """List active projects visible to the user.

    Public projects are visible to everyone; private ones only to their
    creator and whitelisted members (hidden from the list entirely).
    """
    _log.info(
        "list_projects called by user=%s (%s)",
        current_user.user_id, current_user.display_name,
    )
    member_subq = (
        select(ProjectMember.project_id)
        .where(ProjectMember.user_id == current_user.user_id)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Project)
        .where(
            Project.is_active == True,  # noqa: E712
            or_(
                Project.visibility != "private",
                Project.created_by == current_user.user_id,
                Project.project_id.in_(member_subq),
            ),
        )
        .order_by(Project.created_at.asc())
    )
    projects = result.scalars().all()
    _log.info(
        "list_projects returning %d projects: %s",
        len(projects),
        [(p.slug, p.created_by, p.is_active) for p in projects],
    )
    member_ids = await _member_project_ids(
        db, current_user.user_id, [str(p.project_id) for p in projects]
    )
    # Precompute parent membership once (N+1 → 1 extra query).
    child_rows = await db.execute(
        select(Project.project_id).where(
            Project.parent_id.in_([p.project_id for p in projects]),
            Project.is_active == True,  # noqa: E712
        )
    )
    child_ids: set[str] = {str(c) for c in child_rows.scalars().all()}
    # Precompute parent slug map for correct child fs_paths .
    parent_ids = [str(p.parent_id) for p in projects if p.parent_id]
    parent_slugs: dict[str, str] = {}
    if parent_ids:
        parent_rows = await db.execute(
            select(Project.project_id, Project.slug).where(
                Project.project_id.in_(parent_ids),
            )
        )
        parent_slugs = {str(pid): slug for pid, slug in parent_rows.all()}
    return [
        _serialize_project(
            p, current_user.user_id, child_ids,
            parent_slugs.get(str(p.parent_id)), member_ids,
        )
        for p in projects
    ]


def _all_template_count() -> int:
    if not KNOWLEDGE_TEMPLATE_DIR.exists():
        return 0
    return sum(1 for _ in KNOWLEDGE_TEMPLATE_DIR.rglob("*.md"))


# NOTE: must stay registered before GET /{project_id} (bare-str path param).
@router.get("/categories")
async def list_categories(current_user: UserInfo = Depends(get_current_user)):
    """List available project categories for the creation dialog."""
    return [
        {
            "slug": c["slug"],
            "label": c["label"],
            "description": c["description"],
            "template_count": len(get_template_slugs(c["slug"]) or _all_template_count()),
        }
        for c in get_categories()
    ]


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    project: Project = Depends(authorize_project),
    current_user: UserInfo = Depends(get_current_user),
):
    # resolve the parent slug so child projects report their real
    # workspace path (PROJECTS_ROOT/{parent.slug}/projects/{child.slug}).
    parent_slug = None
    if project.parent_id:
        parent_row = await db.execute(
            select(Project.slug).where(Project.project_id == project.parent_id)
        )
        parent_slug = parent_row.scalar_one_or_none()
    member_ids = await _member_project_ids(
        db, current_user.user_id, [project.project_id]
    )
    return _serialize_project(
        project, current_user.user_id,
        parent_slug=parent_slug, member_project_ids=member_ids,
    )


@router.patch("/{project_id}")
async def update_project_visibility(
    project_id: str,
    body: ProjectPatchBody,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Toggle project public/private. Creator-only — members manage the
    whitelist, not the visibility itself."""
    result = await db.execute(
        select(Project).where(
            Project.project_id == project_id,
            Project.is_active == True,  # noqa: E712
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.created_by is None or project.created_by != current_user.user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "PROJECT_NOT_OWNER", "message": "只有项目创建者可以修改项目可见性"},
        )
    project.visibility = body.visibility
    await db.commit()
    await db.refresh(project)
    member_ids = await _member_project_ids(
        db, current_user.user_id, [project.project_id]
    )
    return _serialize_project(
        project, current_user.user_id, member_project_ids=member_ids
    )


@router.get("/{project_id}/mcp-servers")
async def list_mcp_servers(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    project: Project = Depends(authorize_project),
    current_user: UserInfo = Depends(get_current_user),
):
    """Return the MCP servers visible to the project (metadata only).

    Two-tier registry (see ``knowledge/_template/mcp-servers.md``): global
    servers (``mcp_servers.project_id`` NULL, managed via the REST CRUD +
    settings UI) plus project servers lazily synced from
    ``{PROJECTS_ROOT}/{slug}/knowledge/integrations/mcp-servers.md`` — the
    sync runs here so the integration expert's file writes take effect
    without a restart.  A project row shadows a global row with the same
    slug (same rule as agents/skills/workflows); each entry carries its
    ``scope`` so the UI can render the 全局/项目 badge.

    Disabled servers are included with their ``enabled`` flag so the UI can
    show them as "停用".  Sensitive fields (``headers``, secret values) are
    never returned; each server's ``secret_ref`` is checked against
    project_secrets / user_tokens to report whether its credential is
    already configured.
    """
    from api.models.mcp_server import MCPServer
    from api.routers.mcp_servers import _configured_refs, serialize_mcp_server
    from api.services.mcp_sync import sync_mcp_servers_from_file

    # The raw PROJECTS_ROOT/slug join below pointed child projects (workspace
    # at PROJECTS_ROOT/{parent.slug}/projects/{child.slug}) at a nonexistent
    # root-level dir, and the sync silently managed nothing for a missing
    # file — the UI showed an empty catalog. Resolve via the same
    # parent-slug logic as get_project.
    parent_slug = None
    if project.parent_id:
        parent_row = await db.execute(
            select(Project.slug).where(Project.project_id == project.parent_id)
        )
        parent_slug = parent_row.scalar_one_or_none()
    catalog_path = _project_fs_path(project, parent_slug) / "knowledge" / "integrations" / "mcp-servers.md"
    await sync_mcp_servers_from_file(db, project.project_id, catalog_path)

    result = await db.execute(
        select(MCPServer).where(
            or_(MCPServer.project_id == project.project_id, MCPServer.project_id.is_(None))
        )
    )
    rows = result.scalars().all()
    refs = {r.secret_ref for r in rows if r.secret_ref}
    configured_refs = await _configured_refs(
        db, current_user.user_id, project.project_id, refs
    )

    # Project entries shadow global ones with the same slug — the dict
    # keeps the last writer per (sanitized) slug, so global rows must be
    # ordered first and project rows last to win.
    def _rank(r: MCPServer) -> int:
        return 0 if r.project_id is None else 1

    from agent_core.tools._mcp_catalog import sanitize_slug
    winning: dict[str, MCPServer] = {}
    for row in sorted(rows, key=_rank):
        winning[sanitize_slug(row.slug)] = row
    return [
        serialize_mcp_server(
            row,
            row.secret_ref in configured_refs,
            "project" if row.project_id == project.project_id else "global",
        )
        for row in winning.values()
    ]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_route(
    project_id: str,
    body: ProjectDeleteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.project_deletion import DeletionError, delete_project
    try:
        await delete_project(db, project_id, current_user.user_id, body.confirmation)
    except DeletionError as exc:
        raise _error(exc) from exc
    cache = getattr(request.app.state, "knowledge_cache", None)
    if cache is not None:
        cache.invalidate(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
