"""Tests for in-flight user input injection — API layer.

Covers the manager's claim-window buffer, the shared user-message persist
helper (validation / idempotency / sequence order), the interrupted flag on
assistant messages, and the enqueue/watchdog helpers that bridge a queued
injection into the running agent session.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from api.models.conversation import Conversation, Message as DbMessage
from api.routers.conversations import serialize_message
from api.websocket.handlers import (
    _enqueue_injected_message,
    _guard_injected_message,
    _persist_assistant_message,
    _prepare_and_persist_user_message,
)
from api.websocket.manager import ConnectionManager


# ── manager claim-window buffer ────────────────────────────────────

@pytest.fixture
def mgr():
    return ConnectionManager()


def test_pending_injection_buffer_roundtrip(mgr):
    assert not mgr.has_pending_injections("c1")
    mgr.enqueue_pending_injection("c1", {"type": "message", "content": "hi"})
    mgr.enqueue_pending_injection("c1", {"type": "message", "content": "ho"})
    assert mgr.has_pending_injections("c1")
    drained = mgr.drain_pending_injections("c1")
    assert [d["content"] for d in drained] == ["hi", "ho"]
    assert not mgr.has_pending_injections("c1")
    assert mgr.drain_pending_injections("c1") == []


def test_pending_injection_buffer_per_conversation(mgr):
    mgr.enqueue_pending_injection("c1", {"content": "a"})
    mgr.enqueue_pending_injection("c2", {"content": "b"})
    assert [d["content"] for d in mgr.drain_pending_injections("c1")] == ["a"]
    # c2 untouched
    assert mgr.has_pending_injections("c2")


def test_discard_pending_injections(mgr):
    mgr.enqueue_pending_injection("c1", {"content": "a"})
    mgr.discard_pending_injections("c1")
    assert not mgr.has_pending_injections("c1")
    # Idempotent on an empty conversation
    mgr.discard_pending_injections("c1")


def test_session_memories_register_in_manager(mgr):
    """get_session_memories must return the manager's backing list (not an
    orphan) — agent-core memory_rw appends to it in place, and a get()-only
    list would never persist, losing every recall across turns."""
    notes = mgr.get_session_memories("c1")
    notes.append({"note": "remembered", "timestamp": 1.0})
    mgr.add_session_memory("c2", "second", 2.0)
    assert mgr.get_session_memories("c1") == [{"note": "remembered", "timestamp": 1.0}]
    assert [n["note"] for n in mgr.get_session_memories("c2")] == ["second"]
    # 20-note cap with eviction
    for i in range(25):
        mgr.add_session_memory("c3", f"note-{i}", float(i))
    assert [n["note"] for n in mgr.get_session_memories("c3")] == [f"note-{i}" for i in range(5, 25)]


# ── _prepare_and_persist_user_message ──────────────────────────────


async def _project_fs_path(db, project) -> str:
    from api.paths import resolve_project_fs_path
    return await resolve_project_fs_path(str(project.project_id), db)


async def _make_conversation(db, project, title="") -> Conversation:
    """Create a conversation row (the persist helper row-locks it)."""
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
        title=title,
    )
    db.add(conv)
    await db.commit()
    return conv


@pytest.mark.asyncio
async def test_persist_user_message_success(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    result, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "First message", [],
    )
    assert err is None
    assert result.sequence_num == 1
    assert result.attachment_records == []
    row = await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )
    msg = row.scalar_one()
    assert msg.role == "user"
    assert msg.content == "First message"


@pytest.mark.asyncio
async def test_persist_user_message_sets_title_on_first(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project, title=None)
    fs_path = await _project_fs_path(db, project)
    result, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path,
        "第一条消息。继续内容", [],
    )
    assert err is None
    await db.refresh(conv)
    assert conv.title == "第一条消息"


@pytest.mark.asyncio
async def test_persist_user_message_validation_errors(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    # Overlong content
    result, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "x" * 200_001, [],
    )
    assert result is None and "200,000" in err
    # Too many attachments
    atts = [{"url": f"/api/media/{i}.png", "name": f"a{i}", "media_type": "image/png", "size": 1} for i in range(11)]
    result, err = await _prepare_and_persist_user_message(
        db, conv.conversation_id, str(project.project_id), fs_path, "hi", atts,
    )
    assert result is None and "10 attachments" in err
    # Empty / whitespace-only content with no attachments
    for empty in ("", "   ", "\n\t"):
        result, err = await _prepare_and_persist_user_message(
            db, conv.conversation_id, str(project.project_id), fs_path, empty, [],
        )
        assert result is None and "empty" in err, empty
    # Nothing persisted on the failure paths
    count = (await db.execute(
        select(func.count()).select_from(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_persist_user_message_idempotent_on_same_id(db, make_project):
    """Re-persisting the same message_id (watchdog racing forward_events)
    must not duplicate the row or bump the sequence."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    cid = conv.conversation_id  # captured: the helper's rollback expires ORM attrs
    fs_path = await _project_fs_path(db, project)
    result, err = await _prepare_and_persist_user_message(
        db, cid, str(project.project_id), fs_path, "once",
        [], message_id="fixed-inj-id",
    )
    assert err is None and result.sequence_num == 1

    result2, err2 = await _prepare_and_persist_user_message(
        db, cid, str(project.project_id), fs_path, "once",
        [], message_id="fixed-inj-id",
    )
    assert err2 is None
    assert result2.sequence_num == 1  # existing row's seq, not a new one
    count = (await db.execute(
        select(func.count()).select_from(DbMessage).where(DbMessage.conversation_id == cid)
    )).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_sequence_order_assistant_interrupted_then_user(db, make_project):
    """stream_end(interrupted) → user_message_injected → stream_end(final)
    must land in DB with increasing sequence numbers."""
    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    cid = conv.conversation_id
    # 1: interrupted snapshot
    await _persist_assistant_message(db, cid, "partial", [], interrupted=True)
    # 2: injected user message
    result, err = await _prepare_and_persist_user_message(
        db, cid, str(project.project_id), fs_path, "injected", [],
        set_title=False, message_id="inj-seq-1",
    )
    assert err is None and result.sequence_num == 2
    # 3: final assistant message
    await _persist_assistant_message(db, cid, "final answer", [])

    rows = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == cid).order_by(DbMessage.sequence_num)
    )).scalars().all()
    assert [(r.role, r.content) for r in rows] == [
        ("assistant", "partial"),
        ("user", "injected"),
        ("assistant", "final answer"),
    ]


# ── interrupted flag ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_assistant_interrupted_flag_and_serialize(db, make_project):
    project = await make_project()
    conv = await _make_conversation(db, project)
    cid = conv.conversation_id
    await _persist_assistant_message(
        db, cid, "cut short", [], message_id="asst-int",
        interrupted=True,
    )
    row = (await db.execute(
        select(DbMessage).where(DbMessage.message_id == "asst-int")
    )).scalar_one()
    refs = json.loads(row.knowledge_refs)
    assert refs.get("interrupted") is True
    serialized = serialize_message(row)
    assert serialized["interrupted"] is True

    # A normal message stays unflagged
    await _persist_assistant_message(db, cid, "normal", [])
    row2 = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == cid, DbMessage.content == "normal")
    )).scalar_one()
    assert serialize_message(row2)["interrupted"] is False


# ── enqueue / watchdog ─────────────────────────────────────────────


def _make_entry(message_id: str, content: str):
    from agent_core.session import UserInputEntry
    entry = UserInputEntry(message_id=message_id, content=content, attachments=[])
    entry.persisted = asyncio.get_running_loop().create_future()
    return entry


@pytest.mark.asyncio
async def test_enqueue_injected_message_acks_and_queues():
    from agent_core.session import ConversationSession
    from api.websocket.manager import manager

    sess = ConversationSession(conversation_id="c1", project_id="p1", user_id="u1")
    sent: list[dict] = []
    async def _fake_send(conversation_id, data):
        sent.append(data)
        return True
    with patch.object(manager, "send", side_effect=_fake_send):
        await _enqueue_injected_message("c1", sess, {"type": "message", "content": "inject me"})
    assert sent[0]["type"] == "input_queued"
    assert sent[0]["content"] == "inject me"
    assert sent[0]["message_id"]
    assert sess.has_pending_user_input()


@pytest.mark.asyncio
async def test_enqueue_injected_message_rejects_oversized():
    from agent_core.session import ConversationSession
    from api.websocket.manager import manager

    sess = ConversationSession(conversation_id="c1", project_id="p1", user_id="u1")
    sent: list[dict] = []
    async def _fake_send(conversation_id, data):
        sent.append(data)
        return True
    with patch.object(manager, "send", side_effect=_fake_send):
        await _enqueue_injected_message("c1", sess, {"type": "message", "content": "x" * 200_001})
    assert sent[0]["type"] == "input_rejected"
    assert not sess.has_pending_user_input()


@pytest.mark.asyncio
async def test_enqueue_injected_message_queue_full():
    from agent_core.session import ConversationSession
    from api.websocket.manager import manager

    sess = ConversationSession(conversation_id="c1", project_id="p1", user_id="u1")
    for i in range(20):
        entry = _make_entry(f"q-{i}", f"m{i}")
        assert sess.enqueue_user_input(entry)
    sent: list[dict] = []
    async def _fake_send(conversation_id, data):
        sent.append(data)
        return True
    with patch.object(manager, "send", side_effect=_fake_send):
        await _enqueue_injected_message("c1", sess, {"type": "message", "content": "overflow"})
    assert sent[0]["type"] == "input_rejected"
    assert sent[0]["content"] == "overflow"


@pytest.mark.asyncio
async def test_guard_persists_orphan_injection(db, make_project):
    """A message the agent never consumed (turn ended) is persisted as an
    ordinary user message and acknowledged with input_not_processed."""
    from api.websocket.manager import manager

    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    entry = _make_entry("orphan-1", "never consumed")

    sent: list[dict] = []
    async def _fake_send(conversation_id, data):
        sent.append(data)
        return True

    with patch.object(manager, "send", side_effect=_fake_send), \
         patch.object(manager, "get_session", side_effect=[None]) as _m:
        # Session already deregistered → watchdog takes the fallback path
        # without waiting on the 0.2s poll.
        await _guard_injected_message(conv.conversation_id, entry, None)

    assert sent[0]["type"] == "input_not_processed"
    assert sent[0]["message_id"] == "orphan-1"
    row = (await db.execute(
        select(DbMessage).where(DbMessage.conversation_id == conv.conversation_id)
    )).scalar_one()
    assert row.role == "user"
    assert row.content == "never consumed"
    assert row.message_id == "orphan-1"


@pytest.mark.asyncio
async def test_guard_returns_when_consumed():
    from api.websocket.manager import manager

    entry = _make_entry("consumed-1", "got it")
    entry.consumed = True
    with patch.object(manager, "get_session", side_effect=Exception("must not be called")) as _m:
        await _guard_injected_message("c1", entry, None)


@pytest.mark.asyncio
async def test_persist_refuses_soft_deleted_conversation(db, make_project):
    """A delete that raced between the WS checks and the persist row lock
    leaves the conversation soft-deleted; the locked status check must refuse
    the write instead of persisting into an invisible, unrecoverable
    conversation."""
    from sqlalchemy import update as _sa_update

    project = await make_project()
    conv = await _make_conversation(db, project)
    fs_path = await _project_fs_path(db, project)
    # The persist helper rollbacks on refusal, expiring ORM objects — capture
    # the id as a plain string BEFORE calling it.
    cid = str(conv.conversation_id)

    # Simulate the racing delete: row soft-deleted before the row lock.
    await db.execute(
        _sa_update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(status="deleted")
    )
    await db.commit()

    result, err = await _prepare_and_persist_user_message(
        db, cid, str(project.project_id), fs_path,
        "hi there", [],
    )
    assert result is None
    assert err == "Conversation not found"

    # Nothing was written
    rows = (
        await db.execute(
            select(DbMessage).where(DbMessage.conversation_id == cid)
        )
    ).scalars().all()
    assert rows == []
