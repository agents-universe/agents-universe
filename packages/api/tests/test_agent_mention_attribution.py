"""Tests for @-mention agent attribution.

Covers the messages.agent_slug column (user/assistant persistence, API
serialization) and _load_history's [display_name]: annotation that lets an
@-mentioned agent tell its own replies from other agents'.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from api.models.agent import Agent
from api.models.conversation import Conversation, Message as DbMessage
from api.routers.conversations import serialize_message
from api.websocket.handlers import (
    _load_history,
    _persist_assistant_message,
    _prepare_and_persist_user_message,
)


def _rand_slug() -> str:
    # agents.slug is globally unique and startup sync already inserted the
    # framework agents - tests must use slugs that cannot collide.
    return f"agent-{uuid.uuid4().hex[:10]}"


async def _project_fs_path(db, project) -> str:
    from api.paths import resolve_project_fs_path
    return await resolve_project_fs_path(str(project.project_id), db)


async def _make_conversation(db, project) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
    )
    db.add(conv)
    await db.commit()
    return conv


async def _make_agent(db, slug: str, display_name: str) -> Agent:
    agent = Agent(slug=slug, display_name=display_name, definition_path="")
    db.add(agent)
    await db.commit()
    return agent


@pytest.mark.asyncio
async def test_persist_messages_record_agent_slug(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    slug = _rand_slug()

    _, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path,
        "@数据分析专家 看下这个指标", [], agent_slug=slug,
    )
    assert err is None
    await _persist_assistant_message(
        db, conv.conversation_id, "分析结果……", [], agent_slug=slug,
    )
    rows = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
        .order_by(DbMessage.sequence_num)
    )).scalars().all()
    assert [r.agent_slug for r in rows] == [slug, slug]


@pytest.mark.asyncio
async def test_persist_messages_agent_slug_optional(db, make_project):
    """Default turns keep agent_slug NULL (legacy rows stay valid)."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "hi", [],
    )
    await _persist_assistant_message(db, conv.conversation_id, "hello", [])
    rows = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalars().all()
    assert all(r.agent_slug is None for r in rows)


@pytest.mark.asyncio
async def test_serialize_message_includes_agent_slug(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    slug = _rand_slug()
    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], agent_slug=slug,
    )
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    data = serialize_message(row)
    assert data["agent_slug"] == slug


@pytest.mark.asyncio
async def test_load_history_annotates_other_agents(db, make_project):
    """Assistant replies from a different agent get a [display_name]: prefix;
    the current agent's own replies and unattributed rows stay untouched."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    turn_slug = _rand_slug()
    other_slug = _rand_slug()
    await _make_agent(db, other_slug, "数据分析专家")

    await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "question", [],
        agent_slug=turn_slug,
    )
    await _persist_assistant_message(
        db, conv.conversation_id, "other agent's reply", [], agent_slug=other_slug,
    )
    await _persist_assistant_message(
        db, conv.conversation_id, "own reply", [], agent_slug=turn_slug,
    )
    await _persist_assistant_message(db, conv.conversation_id, "legacy reply", [])

    history = await _load_history(
        db, conv.conversation_id,
        Path(fs_path) / ".tmp" / "media" / conv.conversation_id,
        turn_agent_slug=turn_slug,
    )
    assistant_texts = [m.content for m in history if m.role == "assistant"]
    assert "[数据分析专家]:\nother agent's reply" in assistant_texts
    assert "own reply" in assistant_texts
    assert "legacy reply" in assistant_texts


@pytest.mark.asyncio
async def test_load_history_without_turn_agent_slug_no_annotation(db, make_project):
    """turn_agent_slug=None (e.g. a legacy call site) disables attribution."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    other_slug = _rand_slug()
    await _make_agent(db, other_slug, "数据分析专家")
    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], agent_slug=other_slug,
    )
    history = await _load_history(
        db, conv.conversation_id,
        Path(fs_path) / ".tmp" / "media" / conv.conversation_id,
    )
    assert history[0].content == "reply"
