"""Phase/reasoning visibility: heartbeat frames, thinking persistence,
reconnect sync, and the delegation whitelist.

A delegated child has three different fates for the new events: phase frames
(and their heartbeats) must reach the parent's socket so a blocked delegate
stays visible, while thinking must stay out of the parent's bubble and only
land in the child's own messages row."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select
from starlette.websockets import WebSocketDisconnect

from agent_core.agent import Agent
from agent_core.session import ConversationSession
from api.main import app
from api.models.conversation import Conversation, Message as DbMessage
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers.conversations import serialize_message
from api.services.agent_turn import _cap_thinking, _persist_assistant_message
from api.services.delegation import DelegationTransport, _FORWARDED_EVENTS
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message, conversation_ws
from api.websocket.manager import ConnectionManager, manager


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    from agent_core.knowledge.cache import KnowledgeCache
    from agent_core.skills.registry import SkillRegistry
    from agent_core.workflows import WorkflowRegistry

    app.state.knowledge_cache = KnowledgeCache()
    skill_registry = SkillRegistry()
    skill_registry.load_dir(
        str(AGENTS_DIR / "skills"),
        mixin_dir=str(AGENTS_DIR / "_mixins"),
    )
    app.state.skill_registry = skill_registry
    workflow_registry = WorkflowRegistry()
    workflow_registry.load_dir(str(WORKFLOWS_DIR))
    app.state.workflow_registry = workflow_registry
    yield


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


@pytest.fixture
def frames(monkeypatch):
    """Record every frame that reaches the connection manager."""
    seen: list[dict] = []

    async def _send(conversation_id: str, data: dict) -> bool:
        seen.append(data)
        return True

    monkeypatch.setattr(manager, "send", _send)
    return seen


class _Recorder:
    def __init__(self) -> None:
        self.received: list[dict] = []

    async def send(self, conversation_id: str, data: dict) -> bool:
        self.received.append(data)
        return True


# ── _cap_thinking: head+tail truncation before the messages row ──────────


def test_cap_thinking_passthrough():
    assert _cap_thinking("") is None
    assert _cap_thinking("short trace") == "short trace"


def test_cap_thinking_keeps_head_and_tail():
    text = "H" * 12_000 + "M" * 10_000 + "T" * 8_000
    out = _cap_thinking(text)
    # Framing (head) and conclusion (tail) survive; only the middle is cut.
    assert out == "H" * 12_000 + "\n…\n" + text[-8_000:]
    assert out.endswith("T" * 8_000)


def test_cap_thinking_exact_limit_unchanged():
    from api.services.agent_turn import _THINKING_MAX_CHARS

    exact = "x" * _THINKING_MAX_CHARS
    assert _cap_thinking(exact) == exact


# ── delegation whitelist: phase yes, thinking no ─────────────────────────


def test_whitelist_carries_phase_but_not_thinking():
    """turn_status is the parent's only window into a blocked delegate;
    thinking must not hijack the parent's streaming bubble."""
    assert "turn_status" in _FORWARDED_EVENTS
    assert "thinking_delta" not in _FORWARDED_EVENTS
    assert "thinking_end" not in _FORWARDED_EVENTS


async def test_transport_forwards_turn_status_drops_thinking():
    inner = _Recorder()
    transport = DelegationTransport(inner)

    await transport.send("c1", {"type": "turn_status", "phase": "running_tool", "call_id": "t1"})
    await transport.send("c1", {"type": "turn_status", "phase": "running_tool", "heartbeat": True})
    await transport.send("c1", {"type": "thinking_delta", "delta": "child chain"})
    await transport.send("c1", {"type": "thinking_end"})

    forwarded = [e["type"] for e in inner.received]
    assert forwarded == ["turn_status", "turn_status"]
    # Drops must not mark the child's result as failed/errored.
    assert transport.status == "ok"


@pytest.mark.asyncio
async def test_send_turn_error_marks_the_frame_terminal(frames):
    """Early turn-death errors carry ``terminal: True``.

    Nothing streams after them (the claim was released without a
    stream_end), so the client needs the flag to wind down streaming state —
    whereas handlers-side errors that leave the turn running must NOT settle
    the client's live buffer.
    """
    from api.services.agent_turn import _ManagerTransport, _send_turn_error

    payload = {"type": "error", "message": "Conversation not found"}

    class _Ws:
        async def send_json(self, _data):
            pass

    await _send_turn_error(_ManagerTransport(_Ws()), "c1", payload)

    errors = [f for f in frames if f.get("type") == "error"]
    assert errors == [{"type": "error", "message": "Conversation not found", "terminal": True}]
    # the caller's dict is not mutated (call sites pass inline literals)
    assert "terminal" not in payload


# ── persistence + REST serialization ─────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_and_serialize_thinking(db, make_project):
    project = await make_project()
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
    )
    db.add(conv)
    await db.commit()

    await _persist_assistant_message(
        db, conv.conversation_id, "reply", [], thinking="deep dive",
    )
    await _persist_assistant_message(
        db, conv.conversation_id, "plain", [],
    )
    rows = (
        await db.execute(
            select(DbMessage)
            .where(DbMessage.conversation_id == conv.conversation_id)
            .order_by(DbMessage.sequence_num)
        )
    ).scalars().all()
    assert serialize_message(rows[0])["thinking"] == "deep dive"
    # Non-thinking replies stay null so the UI hides the block.
    assert serialize_message(rows[1])["thinking"] is None


# ── reconnect sync replays the streaming thinking + phase ────────────────


class _FakeWS:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000) -> None:
        pass

    def sync_frames(self) -> list[dict]:
        return [f for f in self.sent if f.get("type") == "sync"]


@pytest.fixture
def ws_manager(monkeypatch):
    mgr = ConnectionManager()
    monkeypatch.setattr("api.websocket.handlers.manager", mgr)
    return mgr


@pytest.mark.asyncio
async def test_connect_sync_carries_thinking_and_phase(db, make_project, ws_manager):
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        status="active",
        title="phase sync",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    session.current_streaming_text = "partial "
    session.current_streaming_thinking = "weighing options"
    session.current_turn_phase = "running_tool"
    ws_manager.register_session(conv.conversation_id, session)

    ws = _FakeWS()
    await conversation_ws(conv.conversation_id, ws)
    await session.close()

    sync = ws.sync_frames()[0]
    assert sync["thinking"] == "weighing options"
    assert sync["phase"] == "running_tool"
    # Existing keys keep flowing — the client reads them unconditionally.
    assert sync["streaming_text"] == "partial "


# ── end to end: heartbeat frames during silence + capped persist ─────────


@pytest.mark.asyncio
async def test_silent_turn_gets_heartbeats_and_persists_capped_thinking(
    db, make_project, frames, monkeypatch,
):
    """A waiting model produces no events — the heartbeat must re-send the
    phase anyway, and the accumulated thinking must land in the messages row
    through the head+tail cap."""
    monkeypatch.setattr("api.services.agent_turn._HEARTBEAT_S", 0.05)

    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="hb")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    db.add(UserModelConfig(
        user_id="test-user", provider="openai", model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"), key_hint="...1234",
        url_mode="base_url", complexity_tier=None, sort_order=0,
    ))
    await db.commit()

    msg_id = str(uuid.uuid4())
    head, mid, tail = "H" * 12_000, "M" * 10_000, "T" * 8_000

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            session = kwargs["session"]
            await session.emit("turn_status", phase="waiting_model")
            # Silent window: only heartbeat frames may fill it.
            await asyncio.sleep(0.5)
            await session.emit("thinking_delta", delta=head, message_id=msg_id)
            await session.emit("thinking_delta", delta=mid, message_id=msg_id)
            await session.emit("thinking_delta", delta=tail, message_id=msg_id)
            await session.emit("thinking_end", message_id=msg_id)
            await session.emit("turn_status", phase="responding", message_id=msg_id)
            await session.emit("stream_delta", delta="hello ", message_id=msg_id)
            await session.emit("stream_delta", delta="world", message_id=msg_id)
            await session.emit("stream_end", message_id=msg_id, total_tokens=5)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    await _handle_message(
        conv.conversation_id, SimpleNamespace(app=app),
        {"type": "message", "content": "think hard"}, "test-user",
    )

    errors = [f for f in frames if f.get("type") == "error"]
    assert not errors, errors
    assert sum(f.get("type") == "stream_end" for f in frames) == 1

    heartbeats = [
        f for f in frames
        if f.get("type") == "turn_status" and f.get("heartbeat")
    ]
    assert heartbeats, "silence while waiting on the model must emit heartbeats"
    assert heartbeats[0]["phase"] == "waiting_model"
    # The agent's own phase frame (not a heartbeat) also flows.
    assert any(
        f.get("type") == "turn_status" and not f.get("heartbeat")
        and f.get("phase") == "waiting_model"
        for f in frames
    )

    streamed = "".join(
        f["delta"] for f in frames if f.get("type") == "thinking_delta"
    )
    assert streamed == head + mid + tail
    assert any(f.get("type") == "thinking_end" for f in frames)

    row = (
        await db.execute(select(DbMessage).where(DbMessage.message_id == msg_id))
    ).scalar_one()
    assert row.content == "hello world"
    # Persisted through the cap: head + separator + tail, middle dropped.
    assert row.thinking == head + "\n…\n" + tail
    assert serialize_message(row)["thinking"] == head + "\n…\n" + tail
