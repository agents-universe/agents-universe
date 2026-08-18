"""FastAPI dependencies for authentication and project ownership."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
import redis.exceptions
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("agents_universe.auth")

from api.config import get_settings
from api.database import get_db
from api.models.conversation import Conversation
from api.models.project import Project
from api.models.project_member import ProjectMember
from api.services.redis_client import (
    get_redis,
    get_session,
    renew_session_ttl,
    track_active_user,
)


@dataclass
class UserInfo:
    user_id: str
    display_name: str | None = None


async def get_current_user(
    request: Request,
    redis: Redis = Depends(get_redis),
) -> UserInfo:
    """Validate the session cookie and return the authenticated user."""
    settings = get_settings()
    if settings.auth_bypass_enabled:
        return UserInfo(user_id=settings.auth_bypass_user_id, display_name="SYSTEM")
    session_id = request.cookies.get(settings.auth_cookie_name)
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = await get_session(redis, session_id)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, TimeoutError) as e:
        # a Redis outage must not 500 every authenticated request.
        # Session state cannot be verified — fail closed with 503, never
        # degrade to an anonymous allow.
        _log.warning("Session service unavailable: %s", e)
        raise HTTPException(status_code=503, detail="会话服务暂不可用，请稍后重试")
    if not data or "user_id" not in data:
        raise HTTPException(status_code=401, detail="Session expired")
    try:
        await track_active_user(redis, data["user_id"], settings.active_users_window)
        # Renew session TTL on every authenticated REST request so that API
        # activity (e.g. loading conversation history) keeps the session alive,
        # not just WebSocket messages.
        await renew_session_ttl(redis, session_id, settings.session_ttl)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, TimeoutError) as e:
        # Best-effort tracking/renewal — a failed EXPIRE must not fail the
        # request (the session key itself is still valid until its TTL).
        _log.warning("Session tracking/renewal failed: %s", e)
    return UserInfo(user_id=data["user_id"], display_name=data.get("display_name"))


_PRIVATE_DENIED = {
    "code": "PROJECT_PRIVATE",
    "message": "该项目为私有项目，您没有访问权限",
}


async def is_project_manager(db: AsyncSession, project: Project, user_id: str) -> bool:
    """True when user_id is the project creator or a whitelisted member."""
    if project.created_by is not None and project.created_by == user_id:
        return True
    row = await db.execute(
        select(ProjectMember.project_id).where(
            ProjectMember.project_id == project.project_id,
            ProjectMember.user_id == user_id,
        )
    )
    return row.scalar_one_or_none() is not None


async def has_project_access(db: AsyncSession, project: Project, user_id: str) -> bool:
    """Public projects are open to everyone; private ones need creator/member."""
    if project.visibility != "private":
        return True
    return await is_project_manager(db, project, user_id)


async def authorize_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> Project:
    """Return an active project the current user may access, or 404/403."""
    _log.info(
        "authorize_project: project_id=%s, user=%s",
        project_id, current_user.user_id,
    )
    result = await db.execute(
        select(Project).where(
            Project.project_id == project_id,
            Project.is_active == True,  # noqa: E712
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        _log.warning(
            "authorize_project DENIED: project_id=%s not found or inactive",
            project_id,
        )
        raise HTTPException(status_code=404, detail="Project not found")
    if not await has_project_access(db, project, current_user.user_id):
        _log.warning(
            "authorize_project DENIED: project_id=%s private for user=%s",
            project_id, current_user.user_id,
        )
        raise HTTPException(status_code=403, detail=_PRIVATE_DENIED)
    _log.info(
        "authorize_project OK: project_id=%s, slug=%s, created_by=%s",
        project_id, project.slug, project.created_by,
    )
    return project


async def authorize_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
) -> Conversation:
    """Return a conversation owned by the current user in an active, accessible project, or 404."""
    result = await db.execute(
        select(Conversation, Project)
        .join(Project, Project.project_id == Conversation.project_id)
        .where(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == current_user.user_id,
            Conversation.status == "active",
            Project.is_active == True,  # noqa: E712
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation, project = row
    if not await has_project_access(db, project, current_user.user_id):
        raise HTTPException(status_code=403, detail=_PRIVATE_DENIED)
    return conversation
