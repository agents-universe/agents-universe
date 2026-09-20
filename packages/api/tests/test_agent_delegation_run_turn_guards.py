"""Nested-mode guards in run_turn (G1-G16).

The highest-risk part of delegation: a delegated agent runs ``run_turn`` INSIDE
the parent turn of the same conversation, so every conversation-scoped
singleton — the turn claim, the registered session, the run record, the
upload store, the claim-window injection buffer, the ``agent_tasks`` rows — must
stay untouched by the child. Each assertion below is one guard; removing the
guard makes exactly one of them fail.

The parent turn is driven through the real WS handler with a spy Agent (the
``test_conversation_runs`` harness), and the child through the real
``run_delegated_turn``, so the assertions observe the system as it actually
runs rather than a mock's idea of it.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import AgentTask, Conversation, Message
from api.models.conversation_run import ConversationRun
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers import media as media_router
from api.services.delegation import DelegationContext, run_delegated_turn
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message
from api.websocket.manager import manager


@pytest.fixture(scope="session", autouse=True)
def _app_state_registries():
    """Populate app.state the way the lifespan would (tests never run it)."""
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


@pytest.fixture(autouse=True)
async def _no_leftover_tasks(db):
    """Drop the task rows this file plants.

    The guard under test is that a nested turn leaves the PARENT's task rows
    alone, so the test has to create one — but the startup-recovery path other
    tests exercise sweeps ``agent_tasks`` globally (no conversation filter), so
    a row left behind here would show up in their result sets.
    """
    from sqlalchemy import delete

    yield
    await db.execute(delete(AgentTask))
    await db.commit()


def _write_agent(path, slug: str, *, display_name: str = "", extra: str = "") -> None:
    """Write a definition into a project's agents/ dir (prefix handled by caller)."""
    body = (
        "---\n"
        f"slug: {slug}\n"
        f"display_name: {display_name or slug}\n"
        "description: test agent\n"
        "category: agile-development\n"
        "tools: [filesystem]\n"
        f"{extra}"
        "---\n\n"
        "Test agent body.\n"
    )
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def agent_spy(monkeypatch):
    """Replace Agent.run with a dispatcher keyed on the user message."""
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)
            behavior = state["behavior"]
            if behavior is not None:
                await behavior(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


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


def _ws():
    return SimpleNamespace(app=app)


async def test_nested_turn_touches_no_conversation_scoped_state(agent_spy, db, make_project):
    """One parent turn + one delegated child: the parent keeps everything.

    Guarded: G15 (turn claim), G4 (registered session), G2 (run row),
    G10 (task rows), G13 (injection buffer), G14 (uploads).
    """
    project = await make_project("guardp")
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    # A task the PARENT is running right now: a child's stale-task sweep keyed
    # by conversation (status in pending/running) would flip it to failed.
    db.add(AgentTask(
        conversation_id=conv.conversation_id,
        title="parent task",
        status="running",
    ))
    await db.commit()
    await _add_config(db)

    child_slug = "guardp--helper"
    _write_agent(
        Path(project_fs_path(project)) / "agents" / f"{child_slug}.agent.md", child_slug,
    )
    child_msg_id = str(uuid.uuid4())
    parent_msg_id = str(uuid.uuid4())
    seen: dict = {}

    async def _run(kwargs):
        session = kwargs["session"]
        if kwargs["user_message"].startswith("CHILD:"):
            seen["child_session"] = session
            # Observed from INSIDE the child turn — the state the parent needs
            # back when the child returns. The claim is read off the manager's
            # own set rather than probed with claim_turn: claiming is
            # destructive (it would hand the parent's claim to this test).
            seen["claimed_while_child"] = conv.conversation_id in manager._claimed_turns
            seen["session_while_child"] = manager.get_session(conv.conversation_id)
            seen["uploads_while_child"] = media_router.list_upload_names(conv.conversation_id)
            seen["task_status_while_child"] = await _task_statuses(db, conv.conversation_id)
            await session.emit("stream_delta", delta="child reply")
            await session.emit("stream_end", message_id=child_msg_id, total_tokens=7)
            return

        seen["parent_session"] = session
        # A message buffered for the parent (as _handle_message would have
        # buffered one during the claim window).
        manager.enqueue_pending_injection(conv.conversation_id, {"content": "late"})
        ctx = SimpleNamespace(
            delegation=DelegationContext(
                chain=("guardp--parent",),
                parent_session=session,
                actor_user_id="test-user",
            ),
            app=app,
            project_fs_path=project_fs_path(project),
            conversation_id=conv.conversation_id,
            interactive=True,
        )
        seen["result"] = await run_delegated_turn(
            ctx, agent_slug=child_slug, brief="CHILD: do the thing", reason="no capability",
        )
        seen["injections_after_child"] = manager.has_pending_injections(conv.conversation_id)
        seen["uploads_after_child"] = media_router.list_upload_names(conv.conversation_id)
        seen["claim_after_child"] = await manager.claim_turn(conv.conversation_id)
        # A bare-column query (not an entity query) so this read cannot be
        # answered from the identity map the child's own session never touched.
        seen["task_status_after_child"] = await _task_statuses(db, conv.conversation_id)
        await session.emit("stream_delta", delta="parent final")
        await session.emit("stream_end", message_id=parent_msg_id, total_tokens=3)

    agent_spy["behavior"] = _run
    # Uploaded BEFORE the parent turn starts: the child's turn_started is later
    # than this instant, so its drop_uploads cutoff would delete it.
    media_router.store_upload(conv.conversation_id, "notes.txt", b"keep me")
    # The WS router takes the turn claim before dispatching a frame; handlers.py
    # is only the frame handler, so the test has to claim like the router does
    # for the claim guards to have anything to protect.
    assert await manager.claim_turn(conv.conversation_id) is True
    try:
        await _handle_message(conv.conversation_id, _ws(), {"type": "message", "content": "hi"}, "test-user")
    finally:
        manager.discard_pending_injections(conv.conversation_id)
        media_router.drop_uploads(conv.conversation_id)
        manager.release_turn(conv.conversation_id)

    # G15 — the parent's claim is held for the whole child turn: neither the
    # child's exit path nor anything it calls releases it.
    assert seen["claimed_while_child"] is True
    assert seen["claim_after_child"] is False
    # G4 — the registered session is still the parent's.
    assert seen["session_while_child"] is seen["parent_session"]
    assert seen["session_while_child"] is not seen["child_session"]
    # G14 — uploads survive the child (and the parent's own cleanup, by cutoff).
    assert seen["uploads_while_child"] == ["notes.txt"]
    assert seen["uploads_after_child"] == ["notes.txt"]
    # G10 — the parent's running task row is untouched, both while the child
    # runs and after its exit path (which is where the sweep would fire).
    assert seen["task_status_while_child"] == ["running"]
    assert seen["task_status_after_child"] == ["running"]
    # G13 — the buffered injection still belongs to the parent.
    assert seen["injections_after_child"] is True

    # G2 — one run row: the child wrote none.
    runs = (await db.execute(
        select(ConversationRun).where(ConversationRun.conversation_id == conv.conversation_id)
    )).scalars().all()
    assert len(runs) == 1

    # G16 + visibility — no user row for the brief; the child's reply is its
    # own message carrying its own slug, ordered after the parent's user row.
    rows = (await db.execute(
        select(Message).where(Message.conversation_id == conv.conversation_id).order_by(Message.sequence_num)
    )).scalars().all()
    contents = [r.content for r in rows]
    assert not any("CHILD: do the thing" in c for c in contents), contents
    child_row = next(r for r in rows if r.message_id == child_msg_id)
    assert child_row.agent_slug == child_slug
    assert child_row.content == "child reply"
    user_seq = next(r.sequence_num for r in rows if r.role == "user")
    assert child_row.sequence_num > user_seq
    # The parent ran under the conversation's default agent (the alphabetically
    # first global definition — the test passes no agent_id); all that matters
    # here is that the child's slug did not leak onto the parent's row.
    parent_row = next(r for r in rows if r.message_id == parent_msg_id)
    assert parent_row.content == "parent final"
    assert parent_row.agent_slug and parent_row.agent_slug != child_slug

    # The delegator's contract back to the calling tool.
    result = seen["result"]
    assert result["status"] == "ok"
    assert result["agent"] == child_slug
    assert result["summary"] == "child reply"
    assert result["message_id"] == child_msg_id
    assert result["tokens_used"] == 7


async def test_nested_turn_never_persists_the_brief_as_a_user_message(agent_spy, db, make_project):
    """G16, isolated: the parent's user row is the only user row."""
    project = await make_project("briefp")
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    await _add_config(db)

    child_slug = "briefp--helper"
    _write_agent(Path(project_fs_path(project)) / "agents" / f"{child_slug}.agent.md", child_slug)

    async def _run(kwargs):
        session = kwargs["session"]
        if kwargs["user_message"].startswith("CHILD:"):
            assert kwargs["attachments"] == []
            await session.emit("stream_delta", delta="done")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
            return
        ctx = SimpleNamespace(
            delegation=DelegationContext(
                chain=("briefp--parent",), parent_session=session, actor_user_id="test-user",
            ),
            app=app,
            project_fs_path=project_fs_path(project),
            conversation_id=conv.conversation_id,
            interactive=True,
        )
        await run_delegated_turn(
            ctx, agent_slug=child_slug, brief="CHILD: brief text", reason="r",
        )
        await session.emit("stream_delta", delta="final")
        await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    agent_spy["behavior"] = _run
    await _handle_message(conv.conversation_id, _ws(), {"type": "message", "content": "hello"}, "test-user")

    rows = (await db.execute(
        select(Message).where(Message.conversation_id == conv.conversation_id)
    )).scalars().all()
    users = [r for r in rows if r.role == "user"]
    assert [r.content for r in users] == ["hello"]


async def _task_statuses(db, conversation_id: str) -> list[str]:
    """Task statuses straight from the DB — never through the identity map."""
    return list((await db.execute(
        select(AgentTask.status).where(AgentTask.conversation_id == conversation_id)
    )).scalars().all())


def project_fs_path(project) -> str:
    """The project's workspace dir, as run_turn resolves it."""
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
