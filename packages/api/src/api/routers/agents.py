"""Agents router — list available agents, sync project-scoped definitions."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models.agent import Agent
from api.models.project import Project
from api.paths import resolve_project_fs_path
from api.services.agent_sync import PROJECT_SLUG_SEPARATOR, sync_agents_dir

router = APIRouter(prefix="/api/agents")


class SkillRef(BaseModel):
    slug: str
    description: str = ""


class AgentResponse(BaseModel):
    agent_id: str
    slug: str
    display_name: str
    description: str | None
    category: str
    project_id: str | None = None
    skills: list[SkillRef]
    workflows: list[SkillRef]
    tools: list[str] = []


class SyncResult(BaseModel):
    synced: list[str]
    removed: list[str]


def _parse_refs(raw: str | None) -> list[SkillRef]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
        result = []
        for item in items:
            if isinstance(item, str):
                result.append(SkillRef(slug=item))
            elif isinstance(item, dict):
                result.append(SkillRef(slug=item.get("slug", ""), description=item.get("description", "")))
        return result
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_tool_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
        return [str(t) for t in items if isinstance(t, str)] if isinstance(items, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _to_response(a: Agent) -> AgentResponse:
    return AgentResponse(
        agent_id=a.agent_id,
        slug=a.slug,
        display_name=a.display_name,
        description=a.description,
        category=a.category or "agile-development",
        project_id=a.project_id,
        skills=_parse_refs(a.skills),
        workflows=_parse_refs(a.workflows),
        tools=_parse_tool_list(a.tools),
    )


async def _sync_project_agents(
    db: AsyncSession, project: Project
) -> tuple[list[str], list[str]]:
    """Lazily sync a project's agent definitions from its workspace.

    Runs before listing so the customization expert's file writes take effect
    without a restart. Returns (synced, removed) slug lists.
    """
    fs_path = await resolve_project_fs_path(project.project_id, db)
    return await sync_agents_dir(
        db,
        Path(fs_path) / "agents",
        project_id=project.project_id,
        is_system=False,
        slug_prefix=f"{project.slug}{PROJECT_SLUG_SEPARATOR}",
    )


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    project_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    """List global agents, plus project agents when ``project_id`` is given."""
    if project_id:
        project = await authorize_project(project_id, db, _current_user)
        await _sync_project_agents(db, project)
        result = await db.execute(
            select(Agent)
            .where(or_(Agent.project_id == project_id, Agent.project_id.is_(None)))
            .order_by(Agent.display_name)
        )
    else:
        result = await db.execute(
            select(Agent).where(Agent.project_id.is_(None)).order_by(Agent.display_name)
        )
    return [_to_response(a) for a in result.scalars().all()]


@router.post("/sync", response_model=SyncResult)
async def sync_project_agents(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    """Explicitly re-sync a project's agent definitions from its workspace."""
    project = await authorize_project(project_id, db, _current_user)
    synced, removed = await _sync_project_agents(db, project)
    return SyncResult(synced=synced, removed=removed)
