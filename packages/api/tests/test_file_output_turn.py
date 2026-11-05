"""file_output through a REAL turn — forward_events buffers and persists.

test_file_output.py only drives _persist_assistant_message directly; these
tests drive _handle_message with a spy agent that emits file_output, so the
actual forward_events buffering + stream_end persist path is covered.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation, Message
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message


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


@pytest.fixture
def agent_spy(monkeypatch):
    state: dict = {"behavior": None}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            behavior = state["behavior"]
            if behavior is None:
                return
            await behavior(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


def _ws():
    return SimpleNamespace(app=app)


async def _send(conversation_id: str, msg: dict) -> None:
    await _handle_message(conversation_id, _ws(), msg, "test-user")


async def _make_conversation(db, make_project) -> str:
    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return str(conv.conversation_id), str(project.project_id)


async def _add_config(db) -> None:
    db.add(UserModelConfig(
        user_id="test-user",
        provider="openai",
        model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"),
        key_hint="...1234",
        url_mode="base_url",
        complexity_tier=None,
        sort_order=0,
    ))
    await db.commit()


async def _assistant_messages(db, conversation_id: str) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.sequence_num)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


_FILES = [{
    "id": "a1",
    "url": "/api/media/p/c/report.csv",
    "name": "report.csv",
    "media_type": "text/csv",
    "size": 42,
}]


async def test_turn_file_output_persisted_on_assistant_row(
    agent_spy, db, make_project,
):
    """A normal turn: file_output before stream_end → row carries attachments."""
    cid, _ = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        await session.emit("stream_delta", delta="report ready")
        await session.emit("file_output", message_id=msg_id, files=_FILES)
        await session.emit("stream_end", message_id=msg_id, total_tokens=0)

    agent_spy["behavior"] = _run
    await _send(cid, {"type": "message", "content": "make a report"})

    msgs = await _assistant_messages(db, cid)
    assert len(msgs) == 1
    import json
    refs = json.loads(msgs[0].knowledge_refs or "{}")
    assert refs.get("attachments") == _FILES


async def test_turn_late_file_output_flushed_after_stream_end(
    agent_spy, db, make_project,
):
    """file_output AFTER the terminal stream_end still lands in the row.

    The task-scoped stream_end that follows is skipped by the task_id guard,
    so no in-loop persist trigger exists for the late file. The teardown
    merge at the end of forward_events folds the residual buffers into the
    turn's final row — otherwise the download chips survive only as a
    client-only bubble and vanish on reload/history switch-back.
    """
    cid, _ = await _make_conversation(db, make_project)
    await _add_config(db)
    main_id = str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        await session.emit("stream_delta", delta="done")
        await session.emit("stream_end", message_id=main_id, total_tokens=0)
        # Late deliverable (task-run output) after the main snapshot.
        await session.emit("file_output", message_id=main_id, files=_FILES)
        await session.emit(
            "stream_end", message_id=str(uuid.uuid4()), total_tokens=0,
            task_id="task-1",
        )

    agent_spy["behavior"] = _run
    await _send(cid, {"type": "message", "content": "go"})

    msgs = await _assistant_messages(db, cid)
    assert len(msgs) == 1
    import json
    refs = json.loads(msgs[0].knowledge_refs or "{}")
    assert refs.get("attachments") == _FILES
