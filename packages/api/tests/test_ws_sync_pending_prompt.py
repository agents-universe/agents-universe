"""The WS connect sync must replay prompts still awaiting user input.

Regression: a user_confirm dialog holds no DB row — it exists only in the
client's runtime and in the live session. A client that switched to another
agent and back rebuilds its conversation state from message history, so the
dialog vanished while the agent kept waiting for an answer (the WS sync event
carried streaming text and tool calls, but nothing about the prompt).
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from agent_core.session import ConversationSession
from api.models.conversation import Conversation
from api.websocket.handlers import conversation_ws
from api.websocket.manager import ConnectionManager


class _FakeWS:
    """Just enough WebSocket for the connect handshake, then disconnect."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        # Ends the receive loop the way a client tab-close does.
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000) -> None:
        pass

    def sync_frames(self) -> list[dict]:
        return [f for f in self.sent if f.get("type") == "sync"]


@pytest.fixture
def ws_manager(monkeypatch):
    """Fresh manager — the module-level singleton is shared across tests."""
    mgr = ConnectionManager()
    monkeypatch.setattr("api.websocket.handlers.manager", mgr)
    return mgr


async def _make_conversation(db, make_project) -> Conversation:
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",  # conftest AUTH_BYPASS_USER_ID
        status="active",
        title="prompt replay",
    )
    db.add(conv)
    await db.commit()
    return conv


async def test_connect_replays_a_prompt_awaiting_input(db, make_project, ws_manager):
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    prompt_task = asyncio.create_task(
        session.request_user_selection(
            "prompt-1", "deploy_target", "Which environment?", timeout=30
        )
    )
    await asyncio.sleep(0.05)  # let the prompt register

    ws = _FakeWS()
    await conversation_ws(conv.conversation_id, ws)

    sync = ws.sync_frames()[0]
    prompts = sync["prompts"]
    assert [p["prompt_id"] for p in prompts] == ["prompt-1"]
    assert prompts[0]["question"] == "Which environment?"
    assert prompts[0]["field_key"] == "deploy_target"

    session.resolve_user_selection("prompt-1", "dev")
    assert await prompt_task == "dev"


async def test_connect_without_a_pending_prompt_sends_an_empty_list(db, make_project, ws_manager):
    """The sync frame always carries the field, so the client never has to
    distinguish "no prompts" from "server too old to report them"."""
    conv = await _make_conversation(db, make_project)
    ws_manager.register_session(
        conv.conversation_id,
        ConversationSession(conv.conversation_id, conv.project_id, "test-user"),
    )

    ws = _FakeWS()
    await conversation_ws(conv.conversation_id, ws)

    assert ws.sync_frames()[0]["prompts"] == []


async def test_no_session_means_no_sync_frame(db, make_project, ws_manager):
    """A reconnect after the turn ended has nothing to replay — and must not
    re-arm a dialog for a prompt nobody is waiting on."""
    conv = await _make_conversation(db, make_project)

    ws = _FakeWS()
    await conversation_ws(conv.conversation_id, ws)

    assert ws.sync_frames() == []
