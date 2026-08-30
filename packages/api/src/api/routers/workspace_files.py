"""Workspace file browser — list/read/save files inside a project workspace.

The web UI's "Workspace" tab renders the project's real file tree
(PROJECTS_ROOT/{slug}/) so the user can browse every directory and file,
preview markdown, and edit text files. Scripts (DB automation_scripts +
Playwright specs) are runnable from the same page through the existing
scripts router, so this router only deals with the raw file tree.

Security mirrors the agent-side filesystem tool: every path is resolved
against the project root via agent_core.paths.resolve_within (realpath-based,
so a symlink pointing outside the workspace is rejected), and internal
directories (.git, .tmp) plus hidden dotfiles are never surfaced.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from agent_core.paths import PathEscapeError, resolve_within
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.project import Project

router = APIRouter(prefix="/api/projects/{project_id}/workspace")

_log = logging.getLogger(__name__)

# Byte caps mirror agent_core.tools.filesystem: reading feeds the viewer /
# editor (2MB is already large for a text preview); writing caps disk usage.
_MAX_READ_BYTES = 2_000_000
_MAX_WRITE_BYTES = 5_000_000

# Internal temp/storage directories — never shown in the workspace tree
# (screenshots/generated media live under .tmp/, the git dir is plumbing).
_SKIP_DIRS = {".git", ".tmp"}


def _resolve_target(project_fs: str, rel_path: str) -> Path:
    """Resolve rel_path against the project root, or 400 on escape.

    resolve_within canonicalizes (realpath) both sides, so a symlink inside
    the workspace that points outside is treated as an escape, exactly like
    the agent-side filesystem tool.
    """
    try:
        return resolve_within(Path(project_fs), rel_path)
    except PathEscapeError:
        raise HTTPException(status_code=400, detail="Invalid path")


def _list_entries(base: Path, target: Path) -> list[dict]:
    """Directory entries under *target*, sorted dirs-first then by name.

    Hidden entries (dotfiles) and internal dirs are skipped so the tree
    shows only user-facing project content.
    """
    entries = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        is_dir = entry.is_dir()
        rel = entry.relative_to(base).as_posix()
        stat = entry.stat() if not is_dir else None
        entries.append({
            "name": entry.name,
            "path": rel,
            "type": "dir" if is_dir else "file",
            "size_bytes": stat.st_size if stat else 0,
            "mtime": int(stat.st_mtime) if stat else 0,
        })
    return entries


@router.get("/files")
async def list_workspace_files(
    project_id: str,
    path: str = Query(default="", max_length=2000),
    db=Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """List one directory of the project workspace (relative path from root)."""
    from api.paths import resolve_project_fs_path

    project_fs = await resolve_project_fs_path(project_id, db)
    base = Path(project_fs)
    target = _resolve_target(project_fs, path)
    if not await asyncio.to_thread(target.is_dir):
        raise HTTPException(status_code=404, detail="Directory not found")
    entries = await asyncio.to_thread(_list_entries, base, target)
    return {"path": path, "entries": entries}


@router.get("/file")
async def read_workspace_file(
    project_id: str,
    path: str = Query(..., max_length=2000),
    db=Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Read a text file's raw content (BOM stripped, like the filesystem tool)."""
    from api.paths import resolve_project_fs_path

    project_fs = await resolve_project_fs_path(project_id, db)
    target = _resolve_target(project_fs, path)
    if not await asyncio.to_thread(target.is_file):
        raise HTTPException(status_code=404, detail="File not found")
    stat = await asyncio.to_thread(target.stat)
    if stat.st_size > _MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large to read (>{_MAX_READ_BYTES} bytes)")
    try:
        content = await asyncio.to_thread(target.read_text, "utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 (binary?)")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")
    return {"path": path, "content": content.lstrip("﻿"), "size_bytes": stat.st_size}


class WorkspaceWrite(BaseModel):
    content: str


@router.put("/file")
async def write_workspace_file(
    project_id: str,
    path: str,
    body: WorkspaceWrite,
    background_tasks: BackgroundTasks,
    request: Request,
    db=Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Save a text file into the workspace.

    Writes that land under knowledge/ (a *.md file) additionally snapshot a
    version and re-index in the background so conversations keep serving
    fresh content — mirroring the knowledge router's write path. Frontmatter
    is preserved verbatim (raw write, no parsing).
    """
    if len(body.content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail=f"Content too large (>{_MAX_WRITE_BYTES} bytes)")

    from api.paths import resolve_project_fs_path

    project_fs = await resolve_project_fs_path(project_id, db)
    target = _resolve_target(project_fs, path)
    if await asyncio.to_thread(target.is_dir):
        raise HTTPException(status_code=400, detail="Path is a directory")

    rel = target.relative_to(Path(project_fs))
    # Writes into .git (hooks, config) are refused — same rule as the
    # agent-side filesystem tool: git executes those files unvalidated later.
    if ".git" in rel.parts:
        raise HTTPException(status_code=400, detail=".git 目录为只读")

    # Snapshot the OLD content before overwriting (rollback snapshot, same
    # contract as the knowledge router's PUT version write).
    old_content = ""
    if await asyncio.to_thread(target.exists):
        try:
            old_content = (await asyncio.to_thread(target.read_text, "utf-8")).lstrip("﻿")
        except UnicodeDecodeError:
            old_content = ""

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, body.content, "utf-8")
    except OSError as e:
        raise HTTPException(status_code=507, detail=f"Failed to write file: {e}")

    # Knowledge files: snapshot a version and re-index in the background so
    # the next conversation sees the edited content (same flow as the
    # knowledge router's PUT). Raw write keeps frontmatter intact.
    is_knowledge_md = rel.parts[0] == "knowledge" and target.suffix.lower() == ".md"
    if is_knowledge_md:
        from sqlalchemy import select

        from api.database import AsyncSessionLocal
        from api.models.knowledge import KnowledgeMetadata
        from api.services.knowledge_service import reindex_knowledge, write_knowledge_version

        result = await db.execute(
            select(KnowledgeMetadata).where(
                KnowledgeMetadata.fs_path == str(target),
                KnowledgeMetadata.project_id == project_id,
            )
        )
        km = result.scalar_one_or_none()
        if km:
            try:
                await write_knowledge_version(
                    knowledge_id=str(km.knowledge_id),
                    project_id=project_id,
                    content=old_content,
                    changed_by="user",
                    change_summary="Updated via workspace",
                    db=db,
                )
                await db.commit()
            except Exception:
                _log.exception("Could not write knowledge version for %s", target)
                await db.rollback()

        cache = getattr(request.app.state, "knowledge_cache", None)

        async def _reindex_in_new_session():
            async with AsyncSessionLocal() as new_db:
                await reindex_knowledge(str(target), project_id, new_db)
            if cache is not None:
                cache.invalidate(project_id)

        if background_tasks is not None:
            background_tasks.add_task(_reindex_in_new_session)

    return {"path": path, "saved": True, "bytes_written": len(body.content.encode("utf-8"))}
