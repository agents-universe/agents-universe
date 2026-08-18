"""Guarded conversation soft-delete — the delete/compress/persist race.

A delete's `is_turn_active` check is advisory: a turn can claim the
conversation between that check and the status UPDATE. Only a status='active'
row may be soft-deleted, and the row-locked persist/compress paths must refuse
to write into a conversation that was soft-deleted underneath them.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from api.models.conversation import Conversation, Message as DbMessage
from api.routers.conversations import delete_conversation
from api.services.compression import CompressionError, compress_conversation


async def _make_conversation(db, project, message_count: int = 0) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    if message_count:
        for i in range(message_count):
            db.add(DbMessage(
                conversation_id=str(conv.conversation_id),
                role="user" if i % 2 == 0 else "assistant",
                content=f"message {i} content " * 20,
                sequence_num=i + 1,
            ))
        await db.commit()
    return conv


@pytest.mark.asyncio
async def test_delete_conversation_marks_deleted_and_second_delete_404s(client, db, make_project):
    project = await make_project()
    resp = await client.post(
        f"/api/projects/{project.project_id}/conversations", json={}
    )
    assert resp.status_code == 200, resp.text
    cid = resp.json()["conversation_id"]

    resp = await client.delete(f"/api/conversations/{cid}")
    assert resp.status_code == 200, resp.text

    row = await db.execute(
        select(Conversation).where(Conversation.conversation_id == cid)
    )
    assert row.scalar_one().status == "deleted"

    # A second delete sees status='deleted' via authorize_conversation → 404
    resp = await client.delete(f"/api/conversations/{cid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_guarded_update_refuses_concurrently_deleted(db, make_project):
    """The row was soft-deleted (by a turn racing the delete) after the
    authorize_conversation check passed — the guarded UPDATE must refuse
    instead of deleting a row that is no longer active."""
    project = await make_project()
    conv = await _make_conversation(db, project)

    # Simulate the racing delete: row already soft-deleted, is_turn_active
    # was false at check time (this path bypasses the HTTP dependency).
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conv.conversation_id)
        .values(status="deleted")
    )
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await delete_conversation(str(conv.conversation_id), db, conv)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_compress_refuses_deleted_conversation_http(client, db, make_project):
    """Compress on a soft-deleted conversation is rejected at the dependency
    layer — history rewriting must never target an invisible conversation."""
    project = await make_project()
    conv = await _make_conversation(db, project, message_count=20)

    resp = await client.delete(f"/api/conversations/{conv.conversation_id}")
    assert resp.status_code == 200

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_compress_row_lock_refuses_deleted_before_lock(db, make_project, monkeypatch):
    """Compression passes its pre-checks, then the conversation is
    soft-deleted before the persist row lock — the lock's status filter must
    abort (404) and leave the message history untouched."""
    project = await make_project()
    conv = await _make_conversation(db, project, message_count=20)
    # compress_once rollbacks on refusal, expiring ORM objects — capture the
    # id as a plain string BEFORE calling it.
    cid = str(conv.conversation_id)

    import api.services.compression as compression_service

    from types import SimpleNamespace

    class _StubProvider:
        async def complete(self, messages, tools=None):
            return SimpleNamespace(message=SimpleNamespace(content="summary of earlier conversation"))

    async def _stub_resolve(db_, user_id):
        return _StubProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _stub_resolve)

    # Simulate the racing delete: authorized checks passed, then the row was
    # soft-deleted before the persist lock (bypasses the HTTP dependency).
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(status="deleted")
    )
    await db.commit()

    with pytest.raises(CompressionError) as exc:
        # compress_once wraps this in a singleflight future that nothing
        # retrieves when there is no follower — call the inner function
        # directly to keep the test output clean.
        await compress_conversation(db, cid, "test-user")
    assert exc.value.status_code == 404

    # Nothing was deleted from history
    rows = (
        await db.execute(
            select(DbMessage).where(DbMessage.conversation_id == cid)
        )
    ).scalars().all()
    assert len(rows) == 20
