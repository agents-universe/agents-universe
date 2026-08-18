"""Tests for the manual conversation compression endpoint."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from api.models.conversation import Conversation, Message as DbMessage


def _make_messages(conversation_id: str, count: int) -> list[DbMessage]:
    msgs = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(DbMessage(
            conversation_id=conversation_id,
            role=role,
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    return msgs


class StubProvider:
    def __init__(self, summary: str = "summary of earlier conversation"):
        self._summary = summary

    async def complete(self, messages, tools=None):
        return SimpleNamespace(message=SimpleNamespace(content=self._summary))


async def _seed_conversation(db, project, message_count: int = 20):
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    for m in _make_messages(str(conv.conversation_id), message_count):
        db.add(m)
    await db.commit()
    return conv


@pytest.mark.asyncio
async def test_compress_success(client, db, make_project, monkeypatch):
    project = await make_project()
    conv = await _seed_conversation(db, project, message_count=20)

    import api.services.compression as compression_service

    async def _fake_resolve(db_, user_id):
        return StubProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _fake_resolve)

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["deleted_count"] == 12
    assert data["kept_count"] == 8
    assert "summary of earlier conversation" in data["summary"]

    # Response messages: summary pair first, then the 8 retained messages
    messages = data["messages"]
    assert len(messages) == 10
    assert messages[0]["role"] == "user"
    assert "[Earlier conversation summary" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert messages[2]["content"].startswith("message 12 content")
    assert [m["sequence_num"] for m in messages] == list(range(13, 23))

    # DB now holds exactly the same 10 rows
    result = await db.execute(
        DbMessage.__table__.select().where(
            DbMessage.conversation_id == str(conv.conversation_id)
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_compress_too_short(client, db, make_project, monkeypatch):
    project = await make_project()
    conv = await _seed_conversation(db, project, message_count=5)

    import api.services.compression as compression_service

    monkeypatch.setattr(
        compression_service,
        "_resolve_provider",
        lambda db_, user_id: StubProvider(),
    )

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_compress_rejects_running_agent(client, db, make_project, monkeypatch):
    from api.websocket.manager import manager as ws_manager

    project = await make_project()
    conv = await _seed_conversation(db, project, message_count=20)

    monkeypatch.setattr(ws_manager, "is_session_active", lambda cid: True)

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_compress_llm_failure_leaves_history_intact(client, db, make_project, monkeypatch):
    project = await make_project()
    conv = await _seed_conversation(db, project, message_count=20)

    import api.services.compression as compression_service

    class FailingProvider:
        async def complete(self, messages, tools=None):
            raise RuntimeError("llm down")

    async def _failing_resolve(db_, user_id):
        return FailingProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _failing_resolve)

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 502

    result = await db.execute(
        DbMessage.__table__.select().where(
            DbMessage.conversation_id == str(conv.conversation_id)
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_compress_concurrent_requests_share_one_compression(client, db, make_project, monkeypatch):
    """Two simultaneous POST /compress on the same conversation must not
    duplicate summary pairs, and must not fail with a lock conflict — the
    second request shares the first one's in-flight call (singleflight)."""
    project = await make_project()
    conv = await _seed_conversation(db, project, message_count=20)

    import api.services.compression as compression_service

    calls = {"n": 0}

    class SlowProvider:
        async def complete(self, messages, tools=None):
            calls["n"] += 1
            await asyncio.sleep(0.1)
            return SimpleNamespace(message=SimpleNamespace(content="shared summary"))

    async def _slow_resolve(db_, user_id):
        return SlowProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _slow_resolve)

    url = f"/api/conversations/{conv.conversation_id}/compress"
    resp1, resp2 = await asyncio.gather(client.post(url), client.post(url))
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text
    assert calls["n"] == 1  # singleflight: one LLM call, not two

    result = await db.execute(
        DbMessage.__table__.select().where(
            DbMessage.conversation_id == str(conv.conversation_id)
        )
    )
    rows = result.scalars().all()
    # One summary pair + the 8 retained messages — no duplicated pair.
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_compress_includes_embedded_tool_outputs(client, db, make_project, monkeypatch):
    """Assistant rows persist tool outputs inside tool_calls[i]['output'];
    they must reach the summarization input (as `tool` messages) or the
    output is deleted with the early rows and lost forever."""
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    seen: dict = {}

    class CapturingProvider:
        async def complete(self, messages, tools=None):
            seen["summary_input"] = "\n".join(
                m.content if isinstance(m.content, str) else ""
                for m in messages
            )
            return SimpleNamespace(message=SimpleNamespace(content="summary of earlier conversation"))

    import api.services.compression as compression_service

    async def _capture_resolve(db_, user_id):
        return CapturingProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _capture_resolve)

    # seq 1-11: user/assistant alternation — the tool-calls row must sit in
    # the EARLY segment (seq 1-12), or it is retained and never summarized.
    for i in range(11):
        role = "user" if i % 2 == 0 else "assistant"
        db.add(DbMessage(
            conversation_id=str(conv.conversation_id),
            role=role,
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    # seq 12: assistant message whose tool result lives in tool_calls output
    db.add(DbMessage(
        conversation_id=str(conv.conversation_id),
        role="assistant",
        content="assistant with tool output",
        tool_calls=json.dumps([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
            "output": {"content": "/data/critical.txt"},
        }]),
        sequence_num=12,
    ))
    # seq 13-19: user/assistant alternation; seq 20: final assistant reply
    for i in range(7):
        role = "user" if i % 2 == 0 else "assistant"
        db.add(DbMessage(
            conversation_id=str(conv.conversation_id),
            role=role,
            content=f"tail message {i} content " * 20,
            sequence_num=13 + i,
        ))
    db.add(DbMessage(
        conversation_id=str(conv.conversation_id),
        role="assistant",
        content="final assistant content " * 20,
        sequence_num=20,
    ))
    await db.commit()

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 200, resp.text

    # The embedded tool output reached the summarization LLM
    assert "/data/critical.txt" in seen["summary_input"]


def _make_tool_tail_messages(conversation_id: str) -> list[DbMessage]:
    """20 messages whose recent-8 window opens with an orphan tool message.

    seq 13 is a `tool` result whose assistant tool_calls partner (seq 12)
    falls into the early segment — split_early_recent pops seq 13 back into
    `early`. It is deleted from the DB (the deletion
    boundary shifts up with the popped kept_count) yet must still reach the
    summarization input, or its content (e.g. file paths) is lost forever.
    """
    msgs = []
    for i in range(11):  # seq 1-11: user/assistant alternation
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(DbMessage(
            conversation_id=conversation_id,
            role=role,
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    # seq 12: assistant declaring a tool call; seq 13: its tool result
    msgs.append(DbMessage(
        conversation_id=conversation_id,
        role="assistant",
        content="assistant payload",
        tool_calls=json.dumps([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{}"},
        }]),
        sequence_num=12,
    ))
    msgs.append(DbMessage(
        conversation_id=conversation_id,
        role="tool",
        content="tool result payload /data/x.txt " * 20,
        sequence_num=13,
    ))
    # seq 14-17: two more user/assistant turns
    for i in range(4):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(DbMessage(
            conversation_id=conversation_id,
            role=role,
            content=f"tail message {i} content " * 20,
            sequence_num=14 + i,
        ))
    # seq 18-19: another tool call + result; seq 20: assistant reply
    msgs.append(DbMessage(
        conversation_id=conversation_id,
        role="assistant",
        content="assistant payload 2",
        tool_calls=json.dumps([{
            "id": "call_2",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]),
        sequence_num=18,
    ))
    msgs.append(DbMessage(
        conversation_id=conversation_id,
        role="tool",
        content="tool result payload 2 " * 20,
        sequence_num=19,
    ))
    msgs.append(DbMessage(
        conversation_id=conversation_id,
        role="assistant",
        content="final assistant content " * 20,
        sequence_num=20,
    ))
    return msgs


@pytest.mark.asyncio
async def test_compress_orphan_tool_messages_reach_summary(client, db, make_project, monkeypatch):
    """A tool message popped out of the recent window (its assistant tool_calls
    partner fell into the early segment) is deleted from the DB, so it must be
    part of the summarization input — otherwise its content is lost forever
    ."""
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    seen: dict = {}

    class CapturingProvider:
        async def complete(self, messages, tools=None):
            last = messages[-1]
            seen["summary_input"] = last.content if isinstance(last.content, str) else ""
            return SimpleNamespace(message=SimpleNamespace(content="summary of earlier conversation"))

    import api.services.compression as compression_service

    async def _capture_resolve(db_, user_id):
        return CapturingProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _capture_resolve)

    for m in _make_tool_tail_messages(str(conv.conversation_id)):
        db.add(m)
    await db.commit()

    resp = await client.post(f"/api/conversations/{conv.conversation_id}/compress")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The orphan tool message (seq 13) was popped: 7 kept, 13 deleted
    assert data["kept_count"] == 7
    assert data["deleted_count"] == 13

    # Its content (including the file path) reached the summarization input
    assert "tool result payload" in seen["summary_input"]
    assert "/data/x.txt" in seen["summary_input"]

    # DB holds the summary pair + the 7 retained messages
    from sqlalchemy import select

    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == str(conv.conversation_id))
        .order_by(DbMessage.sequence_num)
    )
    rows = result.scalars().all()
    assert len(rows) == 9
    assert rows[0].content.startswith("[Earlier conversation summary")


@pytest.mark.asyncio
async def test_compress_tool_output_row_inside_recent_window(client, db, make_project, monkeypatch):
    """An assistant row with embedded tool outputs INSIDE the keep window
    expands to 1+N messages. The boundary must be computed from the owning
    ROWS — counting expanded messages lands the boundary one row early,
    leaving a summarized row duplicated in history next to the summary."""
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    import api.services.compression as compression_service

    async def _stub_resolve(db_, user_id):
        return StubProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _stub_resolve)

    cid = str(conv.conversation_id)
    for i in range(17):  # seq 1-17: user/assistant alternation
        db.add(DbMessage(
            conversation_id=cid,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    # seq 18: assistant whose tool result lives in tool_calls output —
    # expands to 2 messages (assistant + tool), inside the keep window
    db.add(DbMessage(
        conversation_id=cid,
        role="assistant",
        content="assistant with tool output",
        tool_calls=json.dumps([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
            "output": {"content": "/data/result.txt"},
        }]),
        sequence_num=18,
    ))
    for i in (18, 19):  # seq 19-20
        db.add(DbMessage(
            conversation_id=cid,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    await db.commit()

    resp = await client.post(f"/api/conversations/{cid}/compress")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 21 expanded messages, 8 kept => rows 14-20 (7 rows) kept, 13 deleted
    assert data["deleted_count"] == 13
    assert data["kept_count"] == 7

    from sqlalchemy import select

    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == cid)
        .order_by(DbMessage.sequence_num)
    )
    rows = result.scalars().all()
    assert len(rows) == 9  # summary pair + 7 retained rows
    assert rows[0].content.startswith("[Earlier conversation summary")
    # seq 13 was summarized away — it must not linger as a row next to the summary
    assert not any("message 12 content" in (r.content or "") for r in rows)


@pytest.mark.asyncio
async def test_compress_all_tool_tail_keeps_last_row(client, db, make_project, monkeypatch):
    """A trailing assistant row with >= RECENT_TURNS_KEEP tool_calls and no
    final text expands to all-tool messages that split_early_recent pops
    EVERYTHING back into early — recent_msgs becomes empty. The old boundary
    then fell on the last row's sequence_num and the DELETE removed the whole
    history (the user's latest turn, e.g. a mid-plan Stop, vanished forever).
    The fallback must retain at least the final row."""
    from agent_core.compressor import RECENT_TURNS_KEEP

    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        token_budget=128000,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    import api.services.compression as compression_service

    async def _stub_resolve(db_, user_id):
        return StubProvider()

    monkeypatch.setattr(compression_service, "_resolve_provider", _stub_resolve)

    cid = str(conv.conversation_id)
    for i in range(17):  # seq 1-17: user/assistant alternation
        db.add(DbMessage(
            conversation_id=cid,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i} content " * 20,
            sequence_num=i + 1,
        ))
    # seq 18: assistant with RECENT_TURNS_KEEP tool_calls and NO final text —
    # the mid-plan-Stop shape. Expands to 1 + N messages, all tool messages
    # land in the last RECENT_TURNS_KEEP and get popped back to early.
    db.add(DbMessage(
        conversation_id=cid,
        role="assistant",
        content="",
        tool_calls=json.dumps([{
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "run_task", "arguments": "{}"},
            "output": {"content": f"task {i} output"},
        } for i in range(RECENT_TURNS_KEEP)]),
        sequence_num=18,
    ))
    await db.commit()

    resp = await client.post(f"/api/conversations/{cid}/compress")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 17 deleted, the final row survives
    assert data["deleted_count"] == 17
    assert data["kept_count"] == 1

    from sqlalchemy import select

    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == cid)
        .order_by(DbMessage.sequence_num)
    )
    rows = result.scalars().all()
    assert len(rows) == 3  # summary pair + the retained final row
    assert rows[0].content.startswith("[Earlier conversation summary")
    assert rows[2].content == ""
    assert rows[2].tool_calls is not None
