"""A confirmation-dialog answer must be recorded as a role="user" message row.

The prompt itself still holds no DB row (see test_ws_sync_pending_prompt.py) —
only the *answer* becomes durable, as `【确认】{question}：{answer}`, so the
conversation history shows what the user confirmed. Plaintext secret values
must never reach `messages` or a broadcast frame.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from agent_core.session import ConversationSession
from api.models.conversation import Conversation, Message
from api.websocket.handlers import conversation_ws
from api.websocket.manager import ConnectionManager
from sqlalchemy import select


class _FakeWS:
    """Connect handshake, then yields scripted frames before disconnecting."""

    def __init__(self, frames: list[dict] | None = None) -> None:
        self.cookies: dict[str, str] = {}
        self.sent: list[dict] = []
        self._frames = list(frames or [])

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def send_text(self, text: str) -> None:
        # ConnectionManager.send() serializes frames via send_text.
        import json

        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        import json

        if self._frames:
            return json.dumps(self._frames.pop(0))
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000) -> None:
        pass

    def frames_of(self, type_: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == type_]


@pytest.fixture
def ws_manager(monkeypatch):
    """Fresh manager — the module-level singleton is shared across tests.

    agent_turn holds its own module-level reference (the broadcast lives
    there), so both bindings must point at the fresh manager or the
    user_selection_recorded frame goes to the unpatched global."""
    mgr = ConnectionManager()
    monkeypatch.setattr("api.websocket.handlers.manager", mgr)
    monkeypatch.setattr("api.services.agent_turn.manager", mgr)
    return mgr


async def _make_conversation(db, make_project) -> Conversation:
    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",  # conftest AUTH_BYPASS_USER_ID
        status="active",
        title="selection recorded",
    )
    db.add(conv)
    await db.commit()
    return conv


async def _start_prompt(session: ConversationSession, prompt_id: str = "prompt-1"):
    task = asyncio.create_task(
        session.request_user_selection(
            prompt_id, "deploy_target", "Which environment?", timeout=30
        )
    )
    await asyncio.sleep(0.05)  # let the prompt register
    return task


async def _user_rows(db, conversation_id: str) -> list[Message]:
    rows = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == "user",
        )
        .order_by(Message.sequence_num)
    )
    return list(rows.scalars().all())


async def test_answer_persisted_as_user_row(db, make_project, ws_manager):
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    prompt_task = await _start_prompt(session)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "value": "dev",
    }])
    await conversation_ws(conv.conversation_id, ws)

    assert await prompt_task == "dev"
    rows = await _user_rows(db, conv.conversation_id)
    assert len(rows) == 1
    assert rows[0].content == "【确认】Which environment?：dev"
    assert rows[0].sequence_num >= 1

    recorded = ws.frames_of("user_selection_recorded")
    assert len(recorded) == 1
    assert recorded[0]["message_id"] == rows[0].message_id
    assert recorded[0]["content"] == rows[0].content


async def test_duplicate_frame_persists_one_row(db, make_project, ws_manager):
    """A double-click / resent frame must not append a second confirmation —
    the uuid5(message_id) hits the IntegrityError-as-success branch."""
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    prompt_task = await _start_prompt(session)

    frame = {
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "value": "dev",
    }
    ws = _FakeWS([frame, dict(frame)])
    await conversation_ws(conv.conversation_id, ws)

    assert await prompt_task == "dev"
    rows = await _user_rows(db, conv.conversation_id)
    assert len(rows) == 1


async def test_cancel_recorded_as_cancelled(db, make_project, ws_manager):
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    prompt_task = await _start_prompt(session)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "value": "__cancelled__",
    }])
    await conversation_ws(conv.conversation_id, ws)

    assert await prompt_task == "__cancelled__"
    rows = await _user_rows(db, conv.conversation_id)
    assert len(rows) == 1
    assert rows[0].content == "【确认】Which environment?：已取消"


async def test_secret_saved_recorded_without_plaintext(db, make_project, ws_manager):
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    prompt_task = await _start_prompt(session)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "secret": True,
        "save_to_project_secrets": True,
        "service_key": "OPENAI_API_KEY",
        "value": "sk-plaintext-must-not-leak",
    }])
    await conversation_ws(conv.conversation_id, ws)

    assert await prompt_task == "secret_saved"
    rows = await _user_rows(db, conv.conversation_id)
    assert len(rows) == 1
    assert rows[0].content == "【确认】Which environment?：已保存密钥"
    assert "sk-plaintext-must-not-leak" not in rows[0].content
    # The broadcast must not carry the plaintext either.
    assert "sk-plaintext-must-not-leak" not in str(ws.sent)


async def test_secret_frame_without_save_flag_writes_no_row(db, make_project, ws_manager):
    """Refused frames must never put the client-supplied value into messages."""
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)
    await _start_prompt(session)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "secret": True,
        "value": "sk-refused-must-not-leak",
    }])
    await conversation_ws(conv.conversation_id, ws)

    rows = await _user_rows(db, conv.conversation_id)
    assert rows == []
    assert ws.frames_of("error")
    assert "sk-refused-must-not-leak" not in str(ws.sent)


async def test_no_session_writes_no_row(db, make_project, ws_manager):
    """With no live session nothing received the answer — keep the existing
    error behavior, no orphan confirmation row."""
    conv = await _make_conversation(db, make_project)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "value": "dev",
    }])
    await conversation_ws(conv.conversation_id, ws)

    rows = await _user_rows(db, conv.conversation_id)
    assert rows == []
    assert ws.frames_of("error")


async def test_secret_saved_without_session_reports_success(db, make_project, ws_manager):
    """The post-session secret-save path must still confirm a SUCCESSFUL save."""
    conv = await _make_conversation(db, make_project)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "secret": True,
        "save_to_project_secrets": True,
        "service_key": "OPENAI_API_KEY",
        "value": "sk-must-not-leak",
    }])
    await conversation_ws(conv.conversation_id, ws)

    infos = ws.frames_of("info")
    assert any(f.get("code") == "SECRET_SAVED" for f in infos)
    assert "sk-must-not-leak" not in str(ws.sent)


async def test_failed_secret_save_without_session_reports_error(db, make_project, ws_manager):
    """saved=False (here: empty value) with no live session must NOT report a
    successful save — the user would believe the credential was stored while
    the next tool call still failed for lack of it."""
    conv = await _make_conversation(db, make_project)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": "prompt-1",
        "secret": True,
        "save_to_project_secrets": True,
        "service_key": "OPENAI_API_KEY",
        # deliberately no "value" → _save_secret_from_response returns False
    }])
    await conversation_ws(conv.conversation_id, ws)

    assert ws.frames_of("error")
    assert not any(f.get("code") == "SECRET_SAVED" for f in ws.frames_of("info"))


async def test_non_string_prompt_id_does_not_kill_the_socket(db, make_project, ws_manager):
    """prompt_id is a dict/list key. A JSON object would raise unhashable
    TypeError, and the outer handler would answer with a generic internal
    error and drop the socket instead of reporting the unknown prompt."""
    conv = await _make_conversation(db, make_project)
    session = ConversationSession(conv.conversation_id, conv.project_id, "test-user")
    ws_manager.register_session(conv.conversation_id, session)

    ws = _FakeWS([{
        "type": "user_selection_response",
        "prompt_id": {"not": "a string"},
        "value": "dev",
    }])
    await conversation_ws(conv.conversation_id, ws)

    messages = [f.get("message", "") for f in ws.frames_of("error")]
    assert any("No pending prompt" in m for m in messages)
    assert not any("internal error" in m for m in messages)
