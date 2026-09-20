"""A delegated agent may still ask the user — and the answer must land.

The child runs its own ``ConversationSession``, which is deliberately NOT
registered with the connection manager (the parent owns that registration). The
WS layer resolves a ``user_selection_response`` through the conversation's
*registered* session, so a prompt registered on the child alone would be
unanswerable: the dialog would appear and every click would be dropped with
"no pending prompt for that prompt_id".

The child's session therefore routes prompts to the top-level session
(``prompt_sink``). These tests drive the real ``conversation_ws`` endpoint with
a fake socket, so the dialog and its answer travel the production path.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation
from api.models.project_secret import ProjectSecret
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.services.delegation import DelegationContext, run_delegated_turn
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message, conversation_ws
from api.websocket.manager import manager


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    from agent_core.knowledge.cache import KnowledgeCache
    from agent_core.skills.registry import SkillRegistry
    from agent_core.workflows import WorkflowRegistry

    app.state.knowledge_cache = KnowledgeCache()
    skill_registry = SkillRegistry()
    skill_registry.load_dir(
        str(AGENTS_DIR / "skills"),
        mixin_dir=str(AGENTS_DIR / "skills" / "_mixins"),
    )
    app.state.skill_registry = skill_registry
    workflow_registry = WorkflowRegistry()
    workflow_registry.load_dir(str(WORKFLOWS_DIR))
    app.state.workflow_registry = workflow_registry
    yield


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    from sqlalchemy import delete

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


class _FakeSocket:
    """Just enough of a WebSocket for conversation_ws to run against."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.frames: asyncio.Queue = asyncio.Queue()
        self.cookies: dict = {}
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def send_text(self, text: str) -> None:
        """The connection manager pushes frames as JSON text, not dicts."""
        import json

        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        return await self.frames.get()

    async def close(self, code: int = 1000) -> None:
        pass

    def types(self) -> list[str]:
        return [f.get("type") for f in self.sent]

    def first(self, event_type: str) -> dict | None:
        return next((f for f in self.sent if f.get("type") == event_type), None)


async def _socket_loop(conv_id: str, ws: _FakeSocket) -> asyncio.Task:
    """Open the real WS endpoint and wait until it is connected."""
    task = asyncio.create_task(conversation_ws(conv_id, ws))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if ws.accepted and manager.get_session(conv_id) is None and conv_id in manager._connections:
            return task
    task.cancel()
    raise AssertionError("conversation_ws never connected")


async def _setup(db, make_project, tag: str):
    project = await make_project(tag)
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    db.add(UserModelConfig(
        user_id="test-user", provider="openai", model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"), key_hint="...1234",
        url_mode="base_url", complexity_tier=None, sort_order=0,
    ))
    await db.commit()
    child_slug = f"{tag}--helper"
    (Path(project_fs(project)) / "agents" / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        f"display_name: {child_slug}\n"
        "description: test agent\n"
        "tools: [filesystem, user_confirm]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    return project, conv, child_slug


def _ctx(session, project, conv):
    return SimpleNamespace(
        delegation=DelegationContext(
            chain=("parent",), parent_session=session, actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )


async def test_delegated_question_reaches_the_user_and_the_answer_lands(
    db, make_project, monkeypatch
):
    project, conv, child_slug = await _setup(db, make_project, "askp")
    seen: dict = {}
    ws = _FakeSocket()

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            session = kwargs["session"]
            if kwargs["user_message"].startswith("CHILD:"):
                seen["child_answer"] = await session.request_user_selection(
                    "prompt-child", "push_confirm", "Push to prod?", timeout=5,
                )
                await session.emit("stream_delta", delta="child pushed")
                await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
                return
            seen["result"] = await run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=child_slug, brief="CHILD: push it", reason="no git tool",
            )
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    ws_task = await _socket_loop(conv.conversation_id, ws)
    try:
        async def _answer_once_the_dialog_shows() -> None:
            for _ in range(400):
                await asyncio.sleep(0.005)
                if ws.first("user_selection_required"):
                    await ws.frames.put(
                        '{"type": "user_selection_response", "prompt_id": "prompt-child", "value": "yes"}'
                    )
                    return
            raise AssertionError("the delegated dialog never reached the client")

        answerer = asyncio.create_task(_answer_once_the_dialog_shows())
        await _handle_message(
            conv.conversation_id, SimpleNamespace(app=app),
            {"type": "message", "content": "hi"}, "test-user",
        )
        await answerer
    finally:
        ws_task.cancel()

    # The dialog the child opened was pushed to the client...
    dialog = ws.first("user_selection_required")
    assert dialog is not None, ws.types()
    assert dialog["prompt_id"] == "prompt-child"
    assert dialog["field_key"] == "push_confirm"
    # ...and the user's answer travelled back into the child's await.
    assert seen["child_answer"] == "yes"
    assert seen["result"]["status"] == "ok"
    assert seen["result"]["summary"] == "child pushed"
    # The dialog is gone once answered — no zombie prompt left behind.
    assert not [f for f in ws.sent if f.get("type") == "user_selection_cancelled"]


async def test_delegated_secret_prompt_saves_and_never_reaches_the_agent(
    db, make_project, monkeypatch
):
    """The secret path resolves through the same registered session."""
    project, conv, child_slug = await _setup(db, make_project, "secp")
    seen: dict = {}
    ws = _FakeSocket()

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            session = kwargs["session"]
            if kwargs["user_message"].startswith("CHILD:"):
                seen["child_answer"] = await session.request_user_selection(
                    "prompt-secret", "git_token", "Git token?", secret=True, timeout=5,
                )
                await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
                return
            await run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=child_slug, brief="CHILD: needs a token", reason="no secret",
            )
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    ws_task = await _socket_loop(conv.conversation_id, ws)
    try:
        async def _answer_once_the_dialog_shows() -> None:
            for _ in range(400):
                await asyncio.sleep(0.005)
                if ws.first("user_selection_required"):
                    await ws.frames.put(
                        '{"type": "user_selection_response", "prompt_id": "prompt-secret",'
                        ' "secret": true, "save_to_project_secrets": true,'
                        ' "service_key": "git", "environment": "test", "value": "s3cret"}'
                    )
                    return
            raise AssertionError("the delegated secret dialog never reached the client")

        answerer = asyncio.create_task(_answer_once_the_dialog_shows())
        await _handle_message(
            conv.conversation_id, SimpleNamespace(app=app),
            {"type": "message", "content": "hi"}, "test-user",
        )
        await answerer
    finally:
        ws_task.cancel()

    # The agent gets an opaque confirmation, never the plaintext.
    assert seen["child_answer"] == "secret_saved"
    assert "secrets_updated" in ws.types()
    row = (await db.execute(
        select(ProjectSecret).where(ProjectSecret.project_id == str(project.project_id))
    )).scalars().first()
    assert row is not None
    assert "s3cret" not in (row.encrypted_value or "")


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
