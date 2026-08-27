"""Tests for conversation activity ordering (conversations.updated_at).

Covers: both message persist paths bumping updated_at, and the list/latest
endpoints ordering by COALESCE(updated_at, created_at) - an old conversation
receiving a new message sorts back to the top instead of staying pinned at
its creation-time position.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation
from api.websocket.handlers import (
    _persist_assistant_message,
    _prepare_and_persist_user_message,
)


async def _project_fs_path(db, project) -> str:
    from api.paths import resolve_project_fs_path
    return await resolve_project_fs_path(str(project.project_id), db)


async def _make_conversation(db, project, created_at: datetime) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
        created_at=created_at,
    )
    db.add(conv)
    await db.commit()
    return conv


async def _reload(db, conv: Conversation) -> Conversation:
    """Fresh row read - the persist paths' commits already expired the ORM
    instance, and the fixture session is where the assertion looks."""
    await db.refresh(conv)
    return conv


@pytest.mark.asyncio
async def test_user_message_persist_bumps_updated_at(db, make_project):
    project = await make_project()
    fs_path = await _project_fs_path(db, project)
    conv = await _make_conversation(
        db, project, datetime.now(timezone.utc) - timedelta(days=1)
    )

    _, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "hi", [],
    )
    assert err is None
    await _reload(db, conv)
    assert conv.updated_at is not None


@pytest.mark.asyncio
async def test_assistant_message_persist_bumps_updated_at(db, make_project):
    project = await make_project()
    conv = await _make_conversation(
        db, project, datetime.now(timezone.utc) - timedelta(days=1)
    )

    await _persist_assistant_message(db, conv.conversation_id, "reply", [])
    await _reload(db, conv)
    assert conv.updated_at is not None


@pytest.mark.asyncio
async def test_list_orders_by_activity_not_creation(db, client, make_project):
    """The old conversation receives a message after the newer one was
    created - it must sort first (creation order would put it last)."""
    project = await make_project()
    fs_path = await _project_fs_path(db, project)
    now = datetime.now(timezone.utc)
    old_conv = await _make_conversation(db, project, now - timedelta(days=1))
    new_conv = await _make_conversation(db, project, now - timedelta(hours=2))

    _, err = await _prepare_and_persist_user_message(
        db, old_conv.conversation_id, str(project.project_id), fs_path, "again", [],
    )
    assert err is None

    resp = await client.get(f"/api/projects/{project.project_id}/conversations")
    assert resp.status_code == 200
    items = resp.json()
    assert [i["conversation_id"] for i in items] == [
        old_conv.conversation_id, new_conv.conversation_id,
    ]
    by_id = {i["conversation_id"]: i for i in items}
    assert by_id[old_conv.conversation_id]["updated_at"] is not None
    assert by_id[new_conv.conversation_id]["updated_at"] is None


@pytest.mark.asyncio
async def test_latest_follows_activity(db, client, make_project):
    """Reload auto-resume picks the conversation used last, not the one
    created last."""
    project = await make_project()
    fs_path = await _project_fs_path(db, project)
    now = datetime.now(timezone.utc)
    old_conv = await _make_conversation(db, project, now - timedelta(days=1))
    await _make_conversation(db, project, now - timedelta(hours=2))

    _, err = await _prepare_and_persist_user_message(
        db, old_conv.conversation_id, str(project.project_id), fs_path, "again", [],
    )
    assert err is None

    resp = await client.get(
        f"/api/projects/{project.project_id}/conversations/latest"
    )
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] == old_conv.conversation_id
