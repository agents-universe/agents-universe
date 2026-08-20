"""Conversation management router."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_conversation, authorize_project, get_current_user
from api.models.agent import Agent
from api.models.conversation import AgentTask, Conversation, Message as DbMessage
from api.models.task_event import TaskEvent
from api.models.project import Project
from api.services.compression import (
    CompressionError,
    compress_once,
)
from api.websocket.manager import manager as ws_manager

router = APIRouter(prefix="/api")


def _safe_payload(raw: str | None) -> dict:
    """Parse a stored task-event payload defensively.

    A corrupted/truncated row (e.g. written by an older buggy version) must
    not 500 the whole timeline endpoint.
    """
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def serialize_message(m: DbMessage) -> dict:
    """Serialize a message row in the shape the frontend consumes."""
    def _parse_refs(m):
        if not m.knowledge_refs:
            return None
        try:
            refs = json.loads(m.knowledge_refs)
            return refs if isinstance(refs, dict) else None
        except (ValueError, TypeError):
            return None

    refs = _parse_refs(m)
    # tool_calls is a JSON column; a corrupted/legacy row must not 500 the
    # whole /messages endpoint (same guard as _parse_refs above).
    try:
        tool_calls = json.loads(m.tool_calls) if m.tool_calls else []
    except (ValueError, TypeError):
        tool_calls = []
    return {
        "message_id": str(m.message_id),
        "role": m.role,
        "content": m.content or "",
        "agent_slug": m.agent_slug,
        "model_name": m.model_name,
        "tool_calls": tool_calls,
        "images": refs and refs.get("images") or None,
        "attachments": refs and refs.get("attachments") or None,
        "interrupted": bool(refs and refs.get("interrupted")),
        "sequence_num": m.sequence_num,
        "created_at": m.created_at.isoformat(),
    }


class ConversationCreate(BaseModel):
    agent_id: str | None = None
    # title column is Unicode(255) — MSSQL DataError (500) on overflow,
    # SQLite silently accepts .
    title: str | None = Field(default=None, max_length=255)
    token_budget: int = Field(default=128000, gt=0, le=2_000_000)


@router.get("/projects/{project_id}/conversations/latest")
async def get_latest_conversation(
    project_id: str,
    agent_slug: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    query = (
        select(Conversation)
        .where(
            Conversation.project_id == project_id,
            Conversation.user_id == current_user.user_id,
            Conversation.status == "active",
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )
    if agent_slug:
        query = query.outerjoin(Agent, Agent.agent_id == Conversation.agent_id).where(
            Agent.slug == agent_slug
        )
    # the else branch filtered `agent_id != None`, hiding
    # agent-less conversations (create_conversation allows agent_id=None)
    # from the "latest" lookup forever — they existed in the DB but could
    # never be resumed from the UI. Without a slug filter, list them all.
    result = await db.execute(query)
    conv = result.scalar_one_or_none()
    if not conv:
        return None
    # Token figures ride along so a page reload restores the ContextMeter
    # immediately — the client has no other cheap source (token_update
    # events only fire while a run is active).
    return {
        "conversation_id": str(conv.conversation_id),
        "tokens_used": conv.tokens_used,
        "token_budget": conv.token_budget,
    }


@router.get("/projects/{project_id}/conversations")
async def list_conversations(
    project_id: str,
    agent_slug: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    message_count_subq = (
        select(func.count(DbMessage.message_id))
        .where(DbMessage.conversation_id == Conversation.conversation_id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    active_task_subq = (
        select(func.count())
        .where(
            AgentTask.conversation_id == Conversation.conversation_id,
            AgentTask.status.in_(["running", "pending"]),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    total_task_subq = (
        select(func.count())
        .where(AgentTask.conversation_id == Conversation.conversation_id)
        .correlate(Conversation)
        .scalar_subquery()
    )

    query = (
        select(
            Conversation,
            Agent.slug.label("agent_slug"),
            message_count_subq.label("message_count"),
            active_task_subq.label("active_task_count"),
            total_task_subq.label("total_task_count"),
        )
        .outerjoin(Agent, Agent.agent_id == Conversation.agent_id)
        .where(
            Conversation.project_id == project_id,
            Conversation.user_id == current_user.user_id,
            Conversation.status == "active",
        )
        .order_by(Conversation.created_at.desc())
        .limit(50)
    )
    if agent_slug:
        query = query.where(Agent.slug == agent_slug)
    # the else branch filtered `agent_id != None`, hiding
    # agent-less conversations (create_conversation allows agent_id=None)
    # from listings. Show all active conversations without a slug filter.

    result = await db.execute(query)
    rows = result.all()
    return [
        {
            "conversation_id": str(row.Conversation.conversation_id),
            "title": row.Conversation.title,
            "agent_id": str(row.Conversation.agent_id) if row.Conversation.agent_id else None,
            "agent_slug": row.agent_slug,
            "token_budget": row.Conversation.token_budget,
            "tokens_used": row.Conversation.tokens_used,
            "message_count": row.message_count,
            "active_task_count": row.active_task_count,
            "total_task_count": row.total_task_count,
            "is_running": ws_manager.is_turn_active(str(row.Conversation.conversation_id)),
            "created_at": row.Conversation.created_at.isoformat(),
        }
        for row in rows
    ]


@router.post("/projects/{project_id}/conversations")
async def create_conversation(
    project_id: str,
    body: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    # body.agent_id is a slug — resolve to the DB primary key.
    # Project-scoped agents are only valid within their own project.
    resolved_agent_id: str | None = None
    if body.agent_id:
        result = await db.execute(select(Agent).where(Agent.slug == body.agent_id))
        db_agent = result.scalar_one_or_none()
        if db_agent is None or (db_agent.project_id is not None and db_agent.project_id != project_id):
            raise HTTPException(status_code=404, detail="Agent not found")
        resolved_agent_id = db_agent.agent_id

    conv = Conversation(
        project_id=project_id,
        user_id=current_user.user_id,
        agent_id=resolved_agent_id,
        title=body.title,
        token_budget=body.token_budget,
    )
    db.add(conv)
    await db.commit()
    return {
        "conversation_id": str(conv.conversation_id),
        "project_id": str(conv.project_id),
        "token_budget": conv.token_budget,
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    conversation: Conversation = Depends(authorize_conversation),
):
    # deleting a conversation with a live turn left the agent
    # running into a soft-deleted (invisible, unrecoverable) conversation —
    # user/assistant messages and episodes kept being written, then the
    # conversation is gone and the deleted row lingers forever. Same 409
    # policy as the compress endpoint.
    if ws_manager.is_turn_active(conversation_id):
        raise HTTPException(status_code=409, detail="Agent 正在运行，请等待完成后再删除。")
    # Guarded update — the is_turn_active check is advisory: a turn can claim
    # the conversation between that check and this UPDATE. Only a
    # status='active' row may be soft-deleted; if a turn re-activated it
    # meanwhile, rowcount stays 0 and the delete refuses instead of racing
    # the running turn.
    result = await db.execute(
        update(Conversation)
        .where(
            Conversation.conversation_id == conversation_id,
            Conversation.status == "active",
        )
        .values(status="deleted")
    )
    if result.rowcount == 0:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Agent 正在运行，请等待完成后再删除。")
    # Commit before the (slow) media rmtree: a turn claimed while the status
    # change was still uncommitted would see status='active', run a full turn,
    # and its reply would land in a conversation deleted moments later —
    # visible to nobody but the tokens were spent.
    await db.commit()
    # Best-effort disk cleanup: uploads and generated images for this
    # conversation live under {PROJECTS_ROOT}/{slug}/.tmp/media/{id}/ and were
    # never reclaimed on delete — soft-deleted conversations' media (privacy
    # and disk growth) lingered forever.
    try:
        from pathlib import Path
        from shutil import rmtree

        from api.paths import resolve_project_fs_path

        project_fs = await resolve_project_fs_path(str(conversation.project_id), db)
        media_dir = Path(project_fs) / ".tmp" / "media" / conversation_id
        if media_dir.exists():
            await asyncio.to_thread(rmtree, media_dir, ignore_errors=True)
    except Exception:
        # The conversation delete itself must never fail because disk
        # cleanup failed — media is best-effort only.
        pass
    # In-memory upload store too: the is_turn_active guard above guarantees no
    # turn is consuming these bytes, so they are dead weight on the global
    # upload quota until the TTL sweep — and privacy data of a deleted
    # conversation should not linger in memory at all.
    try:
        from api.routers.media import drop_uploads
        drop_uploads(conversation_id)
    except Exception:
        pass
    return {"ok": True}


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    conversation: Conversation = Depends(authorize_conversation),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    # Unbounded full-table reads made a long conversation return multi-MB
    # JSON on every history load. Cap the page at 500; the UI loads the most
    # recent messages. The order was ascending, so with >500 messages the
    # newest were never returned — fetch the newest window (desc) and reverse
    # to chronological.
    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num.desc())
        .offset(offset)
        .limit(limit)
    )
    messages = list(result.scalars().all())
    messages.reverse()
    return [serialize_message(m) for m in messages]


@router.post("/conversations/{conversation_id}/compress")
async def compress_conversation_messages(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    conversation: Conversation = Depends(authorize_conversation),
):
    """Summarize the early conversation history and replace it with the summary."""
    if ws_manager.is_turn_active(conversation_id):
        raise HTTPException(status_code=409, detail="Agent 正在运行，请等待完成后再压缩。")
    try:
        # Concurrent compressions of the same conversation share the in-flight
        # call (singleflight) — no lock error, no duplicated summary pairs.
        result = await compress_once(db, conversation_id, current_user.user_id)
    except CompressionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)

    remaining = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num)
    )
    return {
        **result,
        "messages": [serialize_message(m) for m in remaining.scalars().all()],
    }


@router.get("/conversations/{conversation_id}/tasks")
async def list_tasks(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    conversation: Conversation = Depends(authorize_conversation),
):
    # 只返回最新一次 plan 的任务。多次 plan_task 时旧 plan 的行会累积在
    # agent_tasks 里（无 plan 版本列），task_plan_created 事件按 sequence
    # 单调递增，取最后一条解析出任务 id 即可区分新旧 plan。无事件或载荷
    # 损坏（如 _bounded_json 截断后的最小信封）时回退返回全部行。
    latest = await db.execute(
        select(TaskEvent)
        .where(
            TaskEvent.conversation_id == conversation_id,
            TaskEvent.event_type == "task_plan_created",
        )
        .order_by(TaskEvent.sequence.desc())
        .limit(1)
    )
    evt = latest.scalar_one_or_none()
    task_ids: list[str] | None = None
    if evt is not None:
        plan_tasks = _safe_payload(evt.payload).get("tasks")
        if isinstance(plan_tasks, list):
            task_ids = [
                str(t.get("id"))
                for t in plan_tasks
                if isinstance(t, dict) and t.get("id")
            ]
    query = select(AgentTask).where(AgentTask.conversation_id == conversation_id)
    if task_ids:
        query = query.where(AgentTask.task_id.in_(task_ids))
    result = await db.execute(query.order_by(AgentTask.sequence_num))
    tasks = result.scalars().all()
    return [
        {
            "task_id": str(t.task_id),
            "title": t.title,
            "status": t.status,
            "estimated_complexity": t.estimated_complexity,
            "actual_model": t.actual_model,
            "result_summary": t.result_summary,
            "error_message": t.error_message,
            "current_step": t.current_step,
            "next_step": t.next_step,
            "progress_completed": t.progress_completed,
            "progress_total": t.progress_total,
            "created_at": t.created_at.isoformat(),
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in tasks
    ]


@router.get("/conversations/{conversation_id}/task-events")
async def list_task_events(
    conversation_id: str,
    cursor: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    conversation: Conversation = Depends(authorize_conversation),
):
    limit = max(1, min(limit, 200))
    result = await db.execute(
        select(TaskEvent)
        .where(TaskEvent.conversation_id == conversation_id, TaskEvent.sequence > cursor)
        .order_by(TaskEvent.sequence)
        .limit(limit + 1)
    )
    rows = result.scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [
            {
                "event_id": str(event.event_id),
                "sequence": event.sequence,
                "event_type": event.event_type,
                "task_id": event.task_id,
                "payload": _safe_payload(event.payload),
                "created_at": event.created_at.isoformat(),
            }
            for event in rows
        ],
        "next_cursor": rows[-1].sequence if has_more and rows else None,
        "has_more": has_more,
    }


@router.get("/conversations/{conversation_id}/token-usage")
async def token_usage(
    conversation_id: str,
    conversation: Conversation = Depends(authorize_conversation),
):
    return {
        "tokens_used": conversation.tokens_used,
        "token_budget": conversation.token_budget,
        "percent": round(conversation.tokens_used / conversation.token_budget * 100, 1) if conversation.token_budget else 0,
    }
