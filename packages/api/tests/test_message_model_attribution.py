"""Tests for per-message model attribution.

Covers the messages.model_name column (assistant persistence, API
serialization): in auto mode the model_selected event carries the model that
actually executed the turn; explicit selections store the chosen model id.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation, Message as DbMessage
from api.routers.conversations import serialize_message
from api.websocket.handlers import _persist_assistant_message


async def _make_conversation(db, project) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
    )
    db.add(conv)
    await db.commit()
    return conv


@pytest.mark.asyncio
async def test_persist_assistant_message_records_model_name(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], model_name="claude-sonnet-5",
    )
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert row.model_name == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_persist_assistant_message_model_name_optional(db, make_project):
    """Default turns keep model_name NULL (legacy rows stay valid)."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    await _persist_assistant_message(db, conv.conversation_id, "hello", [])
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert row.model_name is None


@pytest.mark.asyncio
async def test_persist_assistant_message_caps_overlong_model_name(db, make_project):
    """model_name is String(100); an overlong id must not DataError the
    persist (same guard as agent_tasks.actual_model)."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], model_name="x" * 300,
    )
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert len(row.model_name) == 100


@pytest.mark.asyncio
async def test_serialize_message_includes_model_name(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], model_name="deepseek-v4-flash",
    )
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    data = serialize_message(row)
    assert data["model_name"] == "deepseek-v4-flash"
