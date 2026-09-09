"""Scheduled task management router (the 定时任务 tab)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.scheduling.cron import CronError, describe_cron, next_run_at, validate_cron
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models._compat import now_utc
from api.models.conversation import Conversation
from api.models.project import Project
from api.models.schedule import ScheduledTask, ScheduledTaskRun
from api.models.script import AutomationScript

router = APIRouter(prefix="/api")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MAX_RUNS = 100

TargetType = Literal["script", "playwright", "agent"]


class ScheduleCreate(BaseModel):
    # Caps mirror the Unicode column widths (MSSQL DataError on overflow).
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    target_type: TargetType
    script_id: str | None = Field(default=None, max_length=36)
    spec_slug: str | None = Field(default=None, max_length=100)
    agent_slug: str | None = Field(default=None, max_length=100)
    prompt: str | None = None
    env: dict[str, str] = Field(default_factory=dict, max_length=16)
    conversation_id: str | None = Field(default=None, max_length=36)
    cron_expr: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    target_type: TargetType | None = None
    script_id: str | None = Field(default=None, max_length=36)
    spec_slug: str | None = Field(default=None, max_length=100)
    agent_slug: str | None = Field(default=None, max_length=100)
    prompt: str | None = None
    env: dict[str, str] | None = Field(default=None, max_length=16)
    conversation_id: str | None = Field(default=None, max_length=36)
    cron_expr: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class CronPreview(BaseModel):
    cron_expr: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


def _task_dict(task: ScheduledTask) -> dict:
    return {
        "schedule_id": str(task.schedule_id),
        "project_id": str(task.project_id),
        "name": task.name,
        "description": task.description,
        "target_type": task.target_type,
        "script_id": str(task.script_id) if task.script_id else None,
        "spec_slug": task.spec_slug,
        "agent_slug": task.agent_slug,
        "prompt": task.prompt,
        "env": json.loads(task.env_json) if task.env_json else {},
        "conversation_id": task.conversation_id,
        "cron_expr": task.cron_expr,
        "schedule": describe_cron(task.cron_expr),
        "timezone": task.timezone,
        "enabled": bool(task.enabled),
        "notify": bool(task.notify),
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_status": task.last_status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


def _run_dict(run: ScheduledTaskRun) -> dict:
    return {
        "run_id": str(run.run_id),
        "schedule_id": str(run.schedule_id),
        "trigger": run.trigger,
        "status": run.status,
        "script_run_id": run.script_run_id,
        "conversation_id": run.conversation_id,
        "summary": run.summary,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _validated_timezone(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail=f"Unknown timezone: {tz}")
    return tz


def _upcoming(cron_expr: str, tz: str, *, enabled: bool) -> datetime | None:
    if not enabled:
        return None
    try:
        return next_run_at(cron_expr, tz, now_utc())
    except CronError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _load_task(db: AsyncSession, schedule_id: str) -> ScheduledTask:
    task = (
        await db.execute(
            select(ScheduledTask).where(ScheduledTask.schedule_id == schedule_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return task


async def _validate_target(
    db: AsyncSession,
    project: Project,
    current_user: UserInfo,
    *,
    target_type: str,
    script_id: str | None,
    spec_slug: str | None,
    agent_slug: str | None,
    prompt: str | None,
    conversation_id: str | None,
    require_conversation: bool,
) -> None:
    """Reject a task whose target could never run (fail at author time)."""
    from api.paths import resolve_project_fs_path
    from api.services.agent_sync import resolve_agent_definition_path

    project_fs = await resolve_project_fs_path(str(project.project_id), db)

    if target_type == "script":
        if not script_id:
            raise HTTPException(status_code=422, detail="script_id is required for a script task")
        script = (
            await db.execute(
                select(AutomationScript).where(
                    AutomationScript.script_id == script_id,
                    AutomationScript.project_id == project.project_id,
                )
            )
        ).scalar_one_or_none()
        if script is None:
            raise HTTPException(status_code=422, detail="Script not found in this project")
        if script.script_type == "playwright":
            raise HTTPException(
                status_code=422,
                detail="Playwright specs are scheduled with target_type=playwright",
            )
    elif target_type == "playwright":
        if not spec_slug or not _SLUG_RE.match(spec_slug):
            raise HTTPException(status_code=422, detail="Invalid spec slug")
        from pathlib import Path
        spec = Path(project_fs) / "tests" / "generated" / f"{spec_slug}.spec.ts"
        try:
            ok = spec.is_file() and spec.resolve().is_relative_to(Path(project_fs).resolve())
        except OSError:
            ok = False
        if not ok:
            raise HTTPException(
                status_code=422, detail=f"Spec not found: tests/generated/{spec_slug}.spec.ts"
            )
    elif target_type == "agent":
        if not agent_slug or not _SLUG_RE.match(agent_slug):
            raise HTTPException(status_code=422, detail="Invalid agent slug")
        if not (prompt or "").strip():
            raise HTTPException(status_code=422, detail="prompt is required for an agent task")
        try:
            resolved = resolve_agent_definition_path(agent_slug, project_fs)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid agent slug")
        if resolved is None:
            raise HTTPException(
                status_code=422, detail=f"No agent definition found for {agent_slug!r}"
            )
    else:
        raise HTTPException(status_code=422, detail=f"Unknown target type: {target_type}")

    if conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.project_id == project.project_id,
                    Conversation.user_id == current_user.user_id,
                    Conversation.status == "active",
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise HTTPException(status_code=422, detail="Target conversation not found")
    elif require_conversation:
        raise HTTPException(status_code=422, detail="conversation_id is required")


@router.get("/projects/{project_id}/schedules")
async def list_schedules(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    rows = (
        await db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.project_id == project_id)
            .order_by(ScheduledTask.created_at.desc())
        )
    ).scalars().all()
    return [_task_dict(t) for t in rows]


@router.post("/projects/{project_id}/schedules/preview")
async def preview_cron(
    project_id: str,
    body: CronPreview,
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Next three fire times for an expression — used by the create dialog."""
    _validated_timezone(body.timezone)
    if validate_cron(body.cron_expr) is not None:
        raise HTTPException(status_code=422, detail=validate_cron(body.cron_expr))
    cursor = now_utc()
    upcoming: list[str] = []
    for _ in range(3):
        try:
            cursor = next_run_at(body.cron_expr, body.timezone, cursor)
        except CronError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        upcoming.append(cursor.isoformat())
    return {"next_runs": upcoming, "description": describe_cron(body.cron_expr)}


@router.post("/projects/{project_id}/schedules")
async def create_schedule(
    project_id: str,
    body: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    _validated_timezone(body.timezone)
    if validate_cron(body.cron_expr) is not None:
        raise HTTPException(status_code=422, detail=validate_cron(body.cron_expr))
    await _validate_target(
        db, project, current_user,
        target_type=body.target_type,
        script_id=body.script_id,
        spec_slug=body.spec_slug,
        agent_slug=body.agent_slug,
        prompt=body.prompt,
        conversation_id=body.conversation_id,
        require_conversation=body.target_type == "agent",
    )

    task = ScheduledTask(
        project_id=project_id,
        name=body.name,
        description=body.description,
        target_type=body.target_type,
        script_id=body.script_id,
        spec_slug=body.spec_slug,
        agent_slug=body.agent_slug,
        prompt=body.prompt,
        env_json=json.dumps(body.env) if body.env else None,
        conversation_id=body.conversation_id,
        cron_expr=body.cron_expr,
        timezone=body.timezone,
        enabled=body.enabled,
        next_run_at=_upcoming(body.cron_expr, body.timezone, enabled=body.enabled),
        created_by=current_user.user_id,
    )
    db.add(task)
    await db.commit()
    return _task_dict(task)


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    task = await _load_task(db, schedule_id)
    await authorize_project(str(task.project_id), db, current_user)
    return _task_dict(task)


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    task = await _load_task(db, schedule_id)
    project = await authorize_project(str(task.project_id), db, current_user)

    target_type = body.target_type or task.target_type
    # Target fields fall back to the stored row so a cron-only PATCH does not
    # need to resend the whole target.
    script_id = body.script_id if body.script_id is not None else task.script_id
    spec_slug = body.spec_slug if body.spec_slug is not None else task.spec_slug
    agent_slug = body.agent_slug if body.agent_slug is not None else task.agent_slug
    prompt = body.prompt if body.prompt is not None else task.prompt
    conversation_id = (
        body.conversation_id if body.conversation_id is not None else task.conversation_id
    )
    cron_expr = body.cron_expr or task.cron_expr
    timezone = body.timezone or task.timezone
    enabled = task.enabled if body.enabled is None else body.enabled

    _validated_timezone(timezone)
    if validate_cron(cron_expr) is not None:
        raise HTTPException(status_code=422, detail=validate_cron(cron_expr))
    await _validate_target(
        db, project, current_user,
        target_type=target_type,
        script_id=script_id,
        spec_slug=spec_slug,
        agent_slug=agent_slug,
        prompt=prompt,
        conversation_id=conversation_id,
        require_conversation=target_type == "agent",
    )

    if body.name is not None:
        task.name = body.name
    if body.description is not None:
        task.description = body.description
    task.target_type = target_type
    task.script_id = script_id if target_type == "script" else None
    task.spec_slug = spec_slug if target_type == "playwright" else None
    task.agent_slug = agent_slug if target_type == "agent" else None
    task.prompt = prompt if target_type == "agent" else None
    if body.env is not None:
        task.env_json = json.dumps(body.env) if body.env else None
    task.conversation_id = conversation_id
    task.cron_expr = cron_expr
    task.timezone = timezone
    task.enabled = enabled
    # Recompute whenever the cadence or the on/off state changed — a stale
    # past next_run_at would fire immediately on the next tick.
    task.next_run_at = _upcoming(cron_expr, timezone, enabled=enabled)
    task.updated_at = now_utc()
    await db.commit()
    return _task_dict(task)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    task = await _load_task(db, schedule_id)
    await authorize_project(str(task.project_id), db, current_user)
    # Cascades to the run history (ScheduledTask.runs).
    await db.delete(task)
    await db.commit()
    return {"deleted": True}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule_now(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.scheduled_runs import ScheduleBusy, spawn_run

    task = await _load_task(db, schedule_id)
    project = await authorize_project(str(task.project_id), db, current_user)
    await _validate_target(
        db, project, current_user,
        target_type=task.target_type,
        script_id=str(task.script_id) if task.script_id else None,
        spec_slug=task.spec_slug,
        agent_slug=task.agent_slug,
        prompt=task.prompt,
        conversation_id=task.conversation_id,
        require_conversation=False,
    )
    try:
        run_id = await spawn_run(request.app, schedule_id, trigger="manual")
    except ScheduleBusy:
        raise HTTPException(status_code=409, detail="This task already has a run in progress")
    return {"run_id": run_id, "status": "pending"}


@router.get("/schedules/{schedule_id}/runs")
async def list_schedule_runs(
    schedule_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    task = await _load_task(db, schedule_id)
    await authorize_project(str(task.project_id), db, current_user)
    rows = (
        await db.execute(
            select(ScheduledTaskRun)
            .where(ScheduledTaskRun.schedule_id == schedule_id)
            .order_by(ScheduledTaskRun.created_at.desc())
            .limit(max(1, min(limit, _MAX_RUNS)))
        )
    ).scalars().all()
    return [_run_dict(r) for r in rows]
