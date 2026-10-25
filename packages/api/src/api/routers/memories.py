"""Memory management router — personal + episodic + session layers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_conversation, authorize_project, get_current_user
from api.errors import api_error
from api.models.conversation import Conversation
from api.models.memory import EpisodicMemory, PersonalMemory
from api.models.project import Project

router = APIRouter(prefix="/api/projects/{project_id}")


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class PersonalMemoryCreate(BaseModel):
    # Caps mirror the Unicode(4000) content column — MSSQL raises DataError
    # (500) on overflow while SQLite silently accepts .
    content: str = Field(max_length=4000)
    tags: list[str] | None = Field(default=None, max_length=20)
    # free-form str accepted arbitrary values ("Project", "GLOBAL",
    # typo'd scopes) which all silently fell through to project-scoped
    # storage via the `== "global"` check — bound to the two real scopes.
    scope: Literal["project", "global"] = "project"

    @field_validator("tags")
    @classmethod
    def validate_tags_serialized_length(cls, v: list[str] | None) -> list[str] | None:
        # max_length only bounds the list COUNT — a single huge tag
        # still overflows the Unicode(500) JSON column on MSSQL. Bound the
        # exact serialized form the DB stores: the writer uses json.dumps'
        # default ensure_ascii=True, so a non-ASCII character costs six
        # characters (\uXXXX) and measuring the raw text would undercount.
        if v and len(json.dumps(v)) > 500:
            raise ValueError("tags too large when serialized (max 500 chars total)")
        return v


class PersonalMemoryUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = Field(default=None, max_length=20)

    @field_validator("tags")
    @classmethod
    def validate_tags_serialized_length(cls, v: list[str] | None) -> list[str] | None:
        if v and len(json.dumps(v)) > 500:
            raise ValueError("tags too large when serialized (max 500 chars total)")
        return v


def _tag_needle(tag: str, dialect_name: str) -> str:
    """Exact JSON encoding of *tag*, escaped for a LIKE ``contains()``.

    The needle must be the tag's exact JSON encoding, not the raw text:
    json.dumps escapes non-ASCII as \\uXXXX and backslash-escapes quotes
    inside a value, so a Chinese tag or one containing '"' would otherwise
    never match the row it was just saved on. LIKE wildcards are escaped so
    a tag like "100%" or "a_b" matches literally instead of as a pattern.
    T-SQL additionally reads ``[abc]`` as a character class with no ESCAPE
    equivalent, so a literal ``[`` must become ``[[]`` on MSSQL only — the
    same reason conversations._like_needle escapes it. Other dialects treat
    ``[`` literally.
    """
    needle = json.dumps(tag)
    escaped = needle.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    if dialect_name == "mssql":
        escaped = escaped.replace("[", "[[]")
    return escaped


# ---------------------------------------------------------------------------
# Personal Memory endpoints
# ---------------------------------------------------------------------------


@router.get("/memories/personal")
async def list_personal_memories(
    project_id: str,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = (
        select(PersonalMemory)
        .where(
            PersonalMemory.user_id == current_user.user_id,
            (PersonalMemory.project_id == project_id) | (PersonalMemory.project_id == None),  # noqa: E711
            PersonalMemory.is_archived == False,  # noqa: E712
        )
        .order_by(PersonalMemory.created_at.desc())
    )
    if tag:
        # The tags column stores a JSON array string; contains() compiles to
        # LIKE '%needle%' — build the needle as the tag's exact JSON encoding
        # with LIKE wildcards escaped (see _tag_needle).
        query = query.where(
            PersonalMemory.tags.contains(
                _tag_needle(tag, db.get_bind().dialect.name), escape="\\"
            )
        )
    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    memories = result.scalars().all()

    out = []
    for m in memories:
        try:
            tags = json.loads(m.tags) if m.tags else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        out.append({
            "memory_id": m.memory_id,
            "content": m.content,
            "tags": tags,
            "created_by": m.created_by,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            "project_id": m.project_id,
        })
    return out


@router.post("/memories/personal", status_code=201)
async def create_personal_memory(
    project_id: str,
    body: PersonalMemoryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    mem = PersonalMemory(
        user_id=current_user.user_id,
        project_id=None if body.scope == "global" else project_id,
        content=body.content,
        tags=json.dumps(body.tags) if body.tags else None,
        created_by="user",
    )
    db.add(mem)
    await db.flush()
    return {
        "memory_id": mem.memory_id,
        "content": mem.content,
        "tags": body.tags or [],
        "created_by": mem.created_by,
        "created_at": mem.created_at.isoformat() if mem.created_at else None,
        "project_id": mem.project_id,
    }


@router.put("/memories/personal/{memory_id}")
async def update_personal_memory(
    project_id: str,
    memory_id: str,
    body: PersonalMemoryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(PersonalMemory).where(
            PersonalMemory.memory_id == memory_id,
            PersonalMemory.user_id == current_user.user_id,
            (PersonalMemory.project_id == project_id) | (PersonalMemory.project_id == None),  # noqa: E711
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        raise api_error(404, "MEMORY_NOT_FOUND", "Memory not found")

    if body.content is not None:
        mem.content = body.content
    if body.tags is not None:
        mem.tags = json.dumps(body.tags)
    mem.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return {"memory_id": mem.memory_id, "updated": True}


@router.delete("/memories/personal/{memory_id}")
async def archive_personal_memory(
    project_id: str,
    memory_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(PersonalMemory).where(
            PersonalMemory.memory_id == memory_id,
            PersonalMemory.user_id == current_user.user_id,
            (PersonalMemory.project_id == project_id) | (PersonalMemory.project_id == None),  # noqa: E711
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        raise api_error(404, "MEMORY_NOT_FOUND", "Memory not found")

    mem.is_archived = True
    await db.flush()
    return {"memory_id": memory_id, "archived": True}


# ---------------------------------------------------------------------------
# Episodic Memory endpoints
# ---------------------------------------------------------------------------


@router.get("/memories/episodic")
async def list_episodic_memories(
    project_id: str,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    query = (
        select(EpisodicMemory)
        .where(
            EpisodicMemory.user_id == current_user.user_id,
            EpisodicMemory.project_id == project_id,
        )
        .order_by(EpisodicMemory.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(query)
    episodes = result.scalars().all()

    out = []
    for ep in episodes:
        try:
            key_findings = json.loads(ep.key_findings) if ep.key_findings else []
        except (json.JSONDecodeError, TypeError):
            key_findings = []
        try:
            open_questions = json.loads(ep.open_questions) if ep.open_questions else []
        except (json.JSONDecodeError, TypeError):
            open_questions = []
        out.append({
            "episode_id": ep.episode_id,
            "conversation_id": ep.conversation_id,
            "summary": ep.summary,
            "key_findings": key_findings,
            "open_questions": open_questions,
            "generated_by": ep.generated_by,
            "created_at": ep.created_at.isoformat() if ep.created_at else None,
        })
    return out


@router.get("/memories/episodic/{episode_id}")
async def get_episodic_memory(
    project_id: str,
    episode_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    result = await db.execute(
        select(EpisodicMemory).where(
            EpisodicMemory.episode_id == episode_id,
            EpisodicMemory.user_id == current_user.user_id,
            EpisodicMemory.project_id == project_id,
        )
    )
    ep = result.scalar_one_or_none()
    if not ep:
        raise api_error(404, "MEMORY_EPISODE_NOT_FOUND", "Episode not found")

    try:
        key_findings = json.loads(ep.key_findings) if ep.key_findings else []
    except (json.JSONDecodeError, TypeError):
        key_findings = []
    try:
        open_questions = json.loads(ep.open_questions) if ep.open_questions else []
    except (json.JSONDecodeError, TypeError):
        open_questions = []

    return {
        "episode_id": ep.episode_id,
        "conversation_id": ep.conversation_id,
        "summary": ep.summary,
        "key_findings": key_findings,
        "open_questions": open_questions,
        "generated_by": ep.generated_by,
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
    }


# ---------------------------------------------------------------------------
# Session Memory endpoint (reads from in-memory storage)
# ---------------------------------------------------------------------------


@router.get("/memories/session/{conversation_id}")
async def get_session_memories(
    project_id: str,
    conversation_id: str,
    project: Project = Depends(authorize_project),
    conversation: Conversation = Depends(authorize_conversation),
):
    if str(conversation.project_id) != project_id:
        raise api_error(404, "CONVERSATION_NOT_FOUND", "Conversation not found")

    from api.websocket.manager import manager
    return manager.get_session_memories(conversation_id)
