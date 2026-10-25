"""Project member whitelist management — who may access a private project.

The creator (projects.created_by) has implicit access and is never stored
here. Members can access the project and manage the whitelist itself
(add/remove other members); the visibility toggle stays creator-only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user, is_project_manager
from api.errors import api_error
from api.models.project import Project
from api.models.project_member import ProjectMember

router = APIRouter(prefix="/api/projects")


class MemberCreateBody(BaseModel):
    # user_id is the SSO `sub` string (String(100)) — there is no username
    # directory, the creator pastes the user_id directly.
    user_id: str = Field(max_length=100)


def _not_manager():
    return api_error(403, "PROJECT_NOT_MEMBER", "Only the project creator or members can manage the access list")


def _member_row(m: ProjectMember) -> dict:
    return {
        "user_id": m.user_id,
        "added_by": m.added_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/{project_id}/members")
async def list_members(
    project_id: str,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """List whitelisted members (managers only — the whitelist itself is
    not public information, even on public projects)."""
    if not await is_project_manager(db, project, current_user.user_id):
        raise _not_manager()
    result = await db.execute(
        select(ProjectMember)
        .where(ProjectMember.project_id == project.project_id)
        .order_by(ProjectMember.created_at.asc())
    )
    return [_member_row(m) for m in result.scalars().all()]


@router.post("/{project_id}/members", status_code=201)
async def add_member(
    project_id: str,
    body: MemberCreateBody,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if not await is_project_manager(db, project, current_user.user_id):
        raise _not_manager()
    uid = body.user_id.strip()
    if not uid:
        raise api_error(400, "INVALID_USER_ID", "user_id cannot be empty")
    if project.created_by is not None and uid == project.created_by:
        raise api_error(400, "MEMBER_IS_OWNER", "The creator already has project access and does not need to join the list")
    existing = await db.execute(
        select(ProjectMember.project_id).where(
            ProjectMember.project_id == project.project_id,
            ProjectMember.user_id == uid,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise api_error(409, "MEMBER_EXISTS", "This user is already on the access list")
    member = ProjectMember(
        project_id=str(project.project_id),
        user_id=uid,
        added_by=current_user.user_id,
    )
    db.add(member)
    try:
        await db.commit()
    except IntegrityError:
        # Check-then-insert race: a concurrent request can win between the
        # SELECT above and the INSERT — surface as 409, not 500.
        await db.rollback()
        raise api_error(409, "MEMBER_EXISTS", "This user is already on the access list")
    await db.refresh(member)
    return _member_row(member)


@router.delete("/{project_id}/members/{user_id}", status_code=204)
async def remove_member(
    project_id: str,
    user_id: str,
    project: Project = Depends(authorize_project),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if not await is_project_manager(db, project, current_user.user_id):
        raise _not_manager()
    if project.created_by is not None and user_id == project.created_by:
        raise api_error(400, "MEMBER_IS_OWNER", "The project creator cannot be removed")
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise api_error(404, "MEMBER_NOT_FOUND", "This user is not on the access list")
    await db.delete(member)
    await db.commit()
