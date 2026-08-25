"""Tests for durable conversation-run tracking (conversation_runs).

Drives _handle_message directly (no socket) with a controllable Agent.run
spy, mirroring test_ws_auto_routing's pattern; the last two tests exercise
the REST surface via the ASGI client.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models._compat import now_utc
from api.models.conversation import Conversation, Message
from api.models.conversation_run import ConversationRun
from api.models.user import UserModelConfig
from api.services.conversation_runs import (
    finish_run,
    interrupt_stale_runs,
    update_run_snapshot,
)
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    """Populate app.state the way the lifespan would (tests never run it)."""
    from api.paths import AGENTS_DIR, WORKFLOWS_DIR
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


@pytest.fixture
def agent_spy(monkeypatch):
    """Replace Agent.run with a spy whose behavior is set per test.

    state["behavior"] is an optional coroutine function receiving the run
    kwargs (session access via kwargs["session"]); the spy emits whatever the
    test schedules, mirroring the real agent's event flow.
    """
    state: dict = {"behavior": None, "run_kwargs": None}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["run_kwargs"] = kwargs
            behavior = state["behavior"]
            if behavior is None:
                return
            await behavior(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    """The suite shares one DB file — drop rows from previous tests."""
    from sqlalchemy import delete

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


async def _make_conversation(db, make_project, project=None):
    project = project or await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _add_config(db) -> str:
    row = UserModelConfig(
        user_id="test-user",
        provider="openai",
        model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"),
        key_hint="...1234",
        url_mode="base_url",
        complexity_tier=None,
        sort_order=0,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.config_id


def _ws():
    return SimpleNamespace(app=app)


async def _send(conversation_id: str, msg: dict) -> None:
    await _handle_message(conversation_id, _ws(), msg, "test-user")


async def _get_run(db, conversation_id: str) -> ConversationRun | None:
    result = await db.execute(
        select(ConversationRun).where(ConversationRun.conversation_id == conversation_id)
    )
    return result.scalars().first()


async def _user_message_id(db, conversation_id: str) -> str:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "user")
        .order_by(Message.sequence_num)
    )
    return result.scalars().first().message_id


# ── run lifecycle via _handle_message ────────────────────────────────────


async def test_run_row_created_and_links_user_message(agent_spy, db, make_project):
    """A turn creates one run row pointing at the persisted user message.

    The spy never emits stream_end, so the finally-tail safety net flips the
    row to interrupted — covering both 3a (creation) and 3g (safety net).
    """
    conv = await _make_conversation(db, make_project)
    await _add_config(db)

    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run is not None
    assert run.user_message_id == await _user_message_id(db, conv.conversation_id)
    assert run.started_at is not None
    assert run.status == "interrupted"
    assert run.ended_at is not None


async def test_run_completed_after_stream_end(agent_spy, db, make_project):
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        await kwargs["session"].emit("stream_delta", delta="partial output ")
        await kwargs["session"].emit("stream_end", message_id=msg_id, total_tokens=42)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "completed"
    assert run.ended_at is not None
    assert run.tokens_used == 42
    # Completed runs leave the Message row authoritative — no snapshot.
    assert run.streaming_snapshot is None


async def test_run_interrupted_on_aborted_stream_end(agent_spy, db, make_project):
    """stop_reason=aborted → interrupted, partial text kept in the snapshot."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        await kwargs["session"].emit("stream_delta", delta="partial output ")
        await kwargs["session"].emit(
            "stream_end", message_id=msg_id, total_tokens=7, stop_reason="aborted"
        )

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "interrupted"
    assert run.streaming_snapshot == "partial output "
    assert run.ended_at is not None


async def test_run_failed_on_agent_exception(agent_spy, db, make_project):
    conv = await _make_conversation(db, make_project)
    await _add_config(db)

    async def _run(kwargs):
        raise RuntimeError("boom")

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "failed"
    assert run.error_message
    assert run.ended_at is not None


async def _assistant_messages(db, conversation_id: str) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.sequence_num)
    )
    return list(result.scalars().all())


async def test_run_failed_and_error_row_on_api_error(agent_spy, db, make_project):
    """provider exception with nothing streamed: the run is failed with the
    error text AND an assistant error row is persisted — the live error
    bubble must survive a reload (the reported "records lost" bug)."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        await kwargs["session"].emit("error", message="LLM API error: 404 model=deepseek-v4-flash")
        await kwargs["session"].emit(
            "stream_end", message_id=msg_id, total_tokens=0, stop_reason="api_error"
        )

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "failed"
    assert "404" in run.error_message

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert "404" in msgs[0].content
    import json as _json
    refs = _json.loads(msgs[0].knowledge_refs)
    assert refs.get("error") is True


async def test_run_failed_on_empty_model_response(agent_spy, db, make_project):
    """A stream_end with no content and no stop_reason (empty model output)
    must not look like a completed turn: run failed with an explanation and
    an error-flagged assistant row fills the void in history — the user sees
    why there is no reply instead of a silently unanswered question."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)

    async def _run(kwargs):
        await kwargs["session"].emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=5)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "failed"
    assert "no output" in run.error_message

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert "no output" in msgs[0].content
    import json as _json
    refs = _json.loads(msgs[0].knowledge_refs)
    assert refs.get("error") is True


async def test_run_failed_with_partial_text_on_api_error(agent_spy, db, make_project):
    """api_error after some text streamed: partial text is persisted as an
    error-flagged row (mirroring the live failStreaming bubble) and the run
    is failed with the error detail."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        await kwargs["session"].emit("stream_delta", delta="partial reply ")
        await kwargs["session"].emit("error", message="LLM API error: timeout")
        await kwargs["session"].emit(
            "stream_end", message_id=msg_id, total_tokens=9, stop_reason="api_error"
        )

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "failed"
    assert "timeout" in run.error_message

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].content == "partial reply "
    import json as _json
    refs = _json.loads(msgs[0].knowledge_refs)
    assert refs.get("error") is True


async def test_run_interrupted_on_task_cancel(agent_spy, db, make_project):
    """User Stop cancels agent_task → the abort path (3e) marks interrupted."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)

    async def _run(kwargs):
        asyncio.current_task().cancel()
        await asyncio.sleep(0)  # deliver the CancelledError

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    run = await _get_run(db, conv.conversation_id)
    assert run.status == "interrupted"
    assert run.ended_at is not None


# ── service helpers ──────────────────────────────────────────────────────


async def test_startup_sweep_interrupts_stale_runs(db, make_project):
    conv = await _make_conversation(db, make_project)
    stale = ConversationRun(conversation_id=conv.conversation_id, status="running")
    done = ConversationRun(conversation_id=conv.conversation_id, status="completed")
    db.add_all([stale, done])
    await db.commit()
    await db.refresh(stale)

    n = await interrupt_stale_runs(db)

    assert n >= 1
    await db.refresh(stale)
    assert stale.status == "interrupted"
    assert stale.ended_at is not None
    await db.refresh(done)
    assert done.status == "completed"
    assert done.ended_at is None


async def test_finish_run_guard_and_snapshot(db, make_project):
    """Terminal writes are one-shot; snapshots stop after the terminal state."""
    conv = await _make_conversation(db, make_project)
    run = ConversationRun(conversation_id=conv.conversation_id)
    db.add(run)
    await db.commit()
    run_id = run.run_id

    await finish_run(run_id, "completed", tokens_used=10)
    # A racing terminal write (e.g. the finally-tail safety net) must no-op.
    await finish_run(run_id, "interrupted", snapshot="late text")
    await db.refresh(run)
    assert run.status == "completed"
    assert run.streaming_snapshot is None
    assert run.tokens_used == 10

    # Snapshot updates also guard on 'running' — dead after the terminal write.
    await update_run_snapshot(run_id, "ignored")
    await db.refresh(run)
    assert run.streaming_snapshot is None


# ── REST surface ─────────────────────────────────────────────────────────


async def test_get_latest_run_endpoint(client, db, make_project):
    conv = await _make_conversation(db, make_project)
    # No runs yet → null.
    resp = await client.get(f"/api/conversations/{conv.conversation_id}/runs/latest")
    assert resp.status_code == 200
    assert resp.json() is None

    older = ConversationRun(
        conversation_id=conv.conversation_id,
        status="interrupted",
        streaming_snapshot="old text",
        # Explicit, distinct timestamps: two rows inserted in one commit can
        # land on the same clock tick on coarse-resolution clocks (Windows),
        # and latest_run's tie-break is run_id DESC — a random coin flip that
        # flakes this test. The ordering is the point, not the tie-break.
        started_at=now_utc() - timedelta(seconds=1),
    )
    newer = ConversationRun(
        conversation_id=conv.conversation_id,
        status="completed",
        user_message_id="um-1",
        tokens_used=42,
        started_at=now_utc(),
    )
    db.add_all([older, newer])
    await db.commit()

    resp = await client.get(f"/api/conversations/{conv.conversation_id}/runs/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == newer.run_id
    assert body["status"] == "completed"
    assert body["user_message_id"] == "um-1"
    assert body["tokens_used"] == 42
    assert body["started_at"]
    assert body["ended_at"] is None
    assert body["error_message"] is None
    assert body["streaming_snapshot"] is None


async def test_list_conversations_includes_last_run_status(client, db, make_project):
    project = await make_project()
    interrupted_conv = await _make_conversation(db, make_project, project)
    clean_conv = await _make_conversation(db, make_project, project)
    db.add(
        ConversationRun(
            conversation_id=interrupted_conv.conversation_id,
            status="interrupted",
        )
    )
    await db.commit()

    resp = await client.get(f"/api/projects/{interrupted_conv.project_id}/conversations")
    assert resp.status_code == 200
    by_id = {item["conversation_id"]: item for item in resp.json()}
    assert by_id[interrupted_conv.conversation_id]["last_run_status"] == "interrupted"
    assert by_id[clean_conv.conversation_id]["last_run_status"] is None
