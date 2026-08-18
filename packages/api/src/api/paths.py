"""Canonical path resolution for bundled resources (agents, workflows, knowledge).

Development: packages/api/src/api/paths.py -> parents[4] = repo root
Docker:      /app/src/api/paths.py         -> parents[2] = /app/
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import get_settings

log = logging.getLogger("agents_universe.paths")


def _find_package_root() -> Path:
    here = Path(__file__).resolve()
    for n in (4, 3, 2):
        if n >= len(here.parents):
            continue
        candidate = here.parents[n]
        if (candidate / "agents").is_dir():
            return candidate
    return Path.cwd()


PACKAGE_ROOT = _find_package_root()

AGENTS_DIR = PACKAGE_ROOT / "agents"
WORKFLOWS_DIR = PACKAGE_ROOT / "workflows"
KNOWLEDGE_TEMPLATE_DIR = PACKAGE_ROOT / "knowledge" / "_template"
# Framework (global) knowledge directory — same directory the indexer scans
# via `python -m agent_core.knowledge.index --global-dir ./knowledge`.
FRAMEWORK_KNOWLEDGE_DIR = PACKAGE_ROOT / "knowledge"

_settings = get_settings()
if not _settings.projects_root:
    raise RuntimeError(
        "PROJECTS_ROOT environment variable is required. "
        "Set it to the directory where sub-project workspaces will be stored "
        "(e.g. PROJECTS_ROOT=/data/projects)."
    )
PROJECTS_ROOT = Path(_settings.projects_root).resolve()

log.info("Package root resolved to: %s", PACKAGE_ROOT)
log.info("Projects root: %s", PROJECTS_ROOT)

if not PROJECTS_ROOT.exists():
    log.warning(
        "PROJECTS_ROOT directory does not exist: %s — "
        "knowledge reads will fail until this path is created or corrected.",
        PROJECTS_ROOT,
    )
elif not PROJECTS_ROOT.is_dir():
    raise RuntimeError(f"PROJECTS_ROOT exists but is not a directory: {PROJECTS_ROOT}")
else:
    _found_dirs = [d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")]
    log.info("Discovered %d project directories under PROJECTS_ROOT: %s", len(_found_dirs), _found_dirs[:20])


async def resolve_project_fs_path(project_id: str, db_session) -> str:
    """Resolve the filesystem path for a project, accounting for parent hierarchy.

    Child projects live under: PROJECTS_ROOT / parent.slug / projects / child.slug
    Root projects live under:  PROJECTS_ROOT / project.slug

    Raises ValueError if the project is not found in the database.
    """
    from sqlalchemy import select
    from api.models.project import Project

    result = await db_session.execute(select(Project).where(Project.project_id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        log.error(
            "resolve_project_fs_path: project_id=%s not found in database",
            project_id,
        )
        raise ValueError(
            f"Project {project_id} not found in the database. "
            f"Cannot resolve filesystem path."
        )

    if project.parent_id:
        parent_result = await db_session.execute(
            select(Project).where(Project.project_id == project.parent_id)
        )
        parent = parent_result.scalar_one_or_none()
        if parent:
            resolved = PROJECTS_ROOT / parent.slug / "projects" / project.slug
        else:
            log.warning(
                "resolve_project_fs_path: parent_id=%s for project '%s' not found, "
                "falling back to root-level path.",
                project.parent_id, project.slug,
            )
            resolved = PROJECTS_ROOT / project.slug
    else:
        resolved = PROJECTS_ROOT / project.slug

    resolved_str = str(resolved)

    if not resolved.exists():
        log.warning(
            "resolve_project_fs_path: resolved path does not exist on disk: %s "
            "(project_id=%s, slug=%s, PROJECTS_ROOT=%s)",
            resolved_str, project_id, project.slug, PROJECTS_ROOT,
        )

    return resolved_str
