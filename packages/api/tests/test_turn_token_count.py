"""Per-turn token attribution: messages.token_count + the turn_token_usage frame.

The conversation ledger (conversations.tokens_used) already accumulated per
turn; these tests pin the attribution layer on top of it — this turn's OWN
delta lands on the turn's final assistant message, and one frame after commit
tells the live client so the badge appears without a history reload.

Drives _handle_message directly with the controllable Agent.run spy (the
test_conversation_runs harness); the nested case adds a real delegated child
(the test_agent_delegation_run_turn_guards harness). No existing test calls
session.add_usage, so the delta>0 gate keeps every legacy assertion untouched.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation, Message
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers.conversations import serialize_message
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
    """The suite shares one DB file — drop rows from previous tests."""
    from sqlalchemy import delete

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


@pytest.fixture
def agent_spy(monkeypatch):
    """Replace Agent.run with a spy whose behavior is set per test."""
    state: dict = {"behavior": None}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            behavior = state["behavior"]
            if behavior is None:
                return
            await behavior(kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


@pytest.fixture
def sent_frames(monkeypatch):
    """Record every frame run_turn pushes to its transport, in order."""
    seen: list[dict] = []
    original = None

    async def _send(transport, conversation_id, data):
        seen.append(dict(data))
        return await original(transport, conversation_id, data)

    import api.services.agent_turn as agent_turn_mod

    original = agent_turn_mod._transport_send
    monkeypatch.setattr(agent_turn_mod, "_transport_send", _send)
    return seen


async def _make_conversation(db, make_project, *, tokens_used: int = 0) -> Conversation:
    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    if tokens_used:
        conv.tokens_used = tokens_used
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


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


async def _send(conversation_id: str, msg: dict) -> None:
    await _handle_message(conversation_id, _ws(), msg, "test-user")


async def _assistant_messages(db, conversation_id: str) -> list[Message]:
    # The turn commits through its own session; populate_existing keeps this
    # identity map from answering from any pre-turn snapshot. (Never expire_all
    # here — expiring `conv` would make the caller's next attribute access a
    # sync lazy load → MissingGreenlet.)
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role == "assistant")
        .order_by(Message.sequence_num)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _fresh_conversation(db, conversation_id: str) -> Conversation:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ── per-turn attribution on the final assistant message ───────────────────


async def test_turn_delta_parked_on_final_message(agent_spy, db, make_project):
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        # Two provider requests in one turn — the delta is the sum.
        session.add_usage(prompt_tokens=30, completion_tokens=12)
        await session.emit("token_update", used=session.tokens_used, budget=session.token_budget)
        session.add_usage(prompt_tokens=10, completion_tokens=5)
        await session.emit("stream_delta", delta="answer")
        await session.emit("stream_end", message_id=msg_id, total_tokens=57)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].token_count == 57
    assert serialize_message(msgs[0])["token_count"] == 57
    fresh = await _fresh_conversation(db, conv.conversation_id)
    assert fresh.tokens_used == 57


async def test_injection_partial_stays_clean_final_carries_turn_total(agent_spy, db, make_project):
    """The frozen partial keeps NULL; the final message carries the WHOLE
    turn's usage, so per-message counts still sum to the conversation ledger."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    frozen_id, final_id = str(uuid.uuid4()), str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        session.add_usage(prompt_tokens=5, completion_tokens=0)
        await session.emit("stream_delta", delta="first half ")
        await session.emit(
            "stream_end", message_id=frozen_id, total_tokens=5,
            stop_reason="interrupted", injection=True,
        )
        session.add_usage(prompt_tokens=7, completion_tokens=0)
        await session.emit("stream_delta", delta="second half")
        await session.emit("stream_end", message_id=final_id, total_tokens=12)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert [m.content for m in msgs] == ["first half ", "second half"]
    frozen = next(m for m in msgs if m.message_id == frozen_id)
    final = next(m for m in msgs if m.message_id == final_id)
    assert frozen.token_count is None
    assert final.token_count == 12
    fresh = await _fresh_conversation(db, conv.conversation_id)
    assert fresh.tokens_used == 12


async def test_aborted_partial_carries_turn_tokens(agent_spy, db, make_project):
    """The abort path falls through to the turn-end write — a stopped turn's
    partial output still reports what it spent."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        session.add_usage(prompt_tokens=20, completion_tokens=4)
        await session.emit("stream_delta", delta="partial output ")
        await session.emit(
            "stream_end", message_id=msg_id, total_tokens=24, stop_reason="aborted"
        )

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].token_count == 24
    fresh = await _fresh_conversation(db, conv.conversation_id)
    assert fresh.tokens_used == 24


async def test_zero_usage_turn_writes_nothing_and_stays_silent(agent_spy, db, make_project, sent_frames):
    """A turn with no provider usage (legacy tests' situation) must not park a
    0 on the message nor push a frame."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        await kwargs["session"].emit("stream_delta", delta="no usage here")
        await kwargs["session"].emit("stream_end", message_id=msg_id, total_tokens=42)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    msgs = await _assistant_messages(db, conv.conversation_id)
    assert len(msgs) == 1
    assert msgs[0].token_count is None
    fresh = await _fresh_conversation(db, conv.conversation_id)
    assert fresh.tokens_used == 0
    assert [f["type"] for f in sent_frames].count("turn_token_usage") == 0


async def test_turn_token_usage_frame_after_stream_end(agent_spy, db, make_project, sent_frames):
    """Exactly one frame, after the stream_end it complements — stream_end was
    forwarded before the token_count write existed."""
    conv = await _make_conversation(db, make_project)
    await _add_config(db)
    msg_id = str(uuid.uuid4())

    async def _run(kwargs):
        session = kwargs["session"]
        session.add_usage(prompt_tokens=8, completion_tokens=2)
        await session.emit("stream_delta", delta="reply")
        await session.emit("stream_end", message_id=msg_id, total_tokens=10)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hello"})

    token_frames = [f for f in sent_frames if f["type"] == "turn_token_usage"]
    assert len(token_frames) == 1
    assert token_frames[0]["message_id"] == msg_id
    assert token_frames[0]["token_count"] == 10
    types = [f["type"] for f in sent_frames]
    assert types.index("turn_token_usage") > types.index("stream_end")


# ── delegation: child and parent attribute their own spend ────────────────


def _write_agent(path, slug: str) -> None:
    path.write_text(
        "---\n"
        f"slug: {slug}\n"
        f"display_name: {slug}\n"
        "description: test agent\n"
        "category: agile-development\n"
        "tools: [filesystem]\n"
        "---\n\n"
        "Test agent body.\n",
        encoding="utf-8",
    )


def _project_fs_path(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)


@pytest.fixture
def ws_frames(monkeypatch):
    """Record every frame that reaches the connection manager."""
    seen: list[dict] = []

    async def _send(conversation_id: str, data: dict) -> bool:
        seen.append(dict(data))
        return True

    monkeypatch.setattr(manager, "send", _send)
    return seen


async def test_nested_turn_splits_attribution_and_reports_child_delta(
    agent_spy, db, make_project, ws_frames,
):
    """Each message carries its own turn's delta; the ledger sums to both;
    the delegator reports the child's OWN cost, not its cumulative ledger.

    The conversation starts at 100 lifetime tokens so any cumulative/delta
    confusion shows up as an inflated number instead of passing silently.
    """
    project = await make_project("tokp")
    conv = Conversation(
        user_id="test-user", project_id=project.project_id, title="t", tokens_used=100,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    await _add_config(db)

    child_slug = "tokp--helper"
    _write_agent(Path(_project_fs_path(project)) / "agents" / f"{child_slug}.agent.md", child_slug)
    child_msg_id = str(uuid.uuid4())
    parent_msg_id = str(uuid.uuid4())
    seen: dict = {}

    async def _run(kwargs):
        session = kwargs["session"]
        if kwargs["user_message"].startswith("CHILD:"):
            session.add_usage(prompt_tokens=10, completion_tokens=5)
            await session.emit("stream_delta", delta="child reply")
            await session.emit("stream_end", message_id=child_msg_id, total_tokens=115)
            return
        ctx = SimpleNamespace(
            delegation=DelegationContext(
                chain=("tokp--parent",),
                parent_session=session,
                actor_user_id="test-user",
            ),
            app=app,
            project_fs_path=_project_fs_path(project),
            conversation_id=conv.conversation_id,
            interactive=True,
        )
        seen["result"] = await run_delegated_turn(
            ctx, agent_slug=child_slug, brief="CHILD: do it", reason="no capability",
        )
        session.add_usage(prompt_tokens=3, completion_tokens=2)
        await session.emit("stream_delta", delta="parent final")
        await session.emit("stream_end", message_id=parent_msg_id, total_tokens=105)

    agent_spy["behavior"] = _run
    await _send(conv.conversation_id, {"type": "message", "content": "hi"})

    msgs = await _assistant_messages(db, conv.conversation_id)
    child_row = next(m for m in msgs if m.message_id == child_msg_id)
    parent_row = next(m for m in msgs if m.message_id == parent_msg_id)
    assert child_row.token_count == 15
    assert parent_row.token_count == 5

    fresh = await _fresh_conversation(db, conv.conversation_id)
    assert fresh.tokens_used == 120  # 100 lifetime + 15 child + 5 parent

    # The delegator reports the child's own cost (15), never the cumulative
    # ledger it would read off stream_end.total_tokens (115).
    assert seen["result"]["tokens_used"] == 15
    finished = next(f for f in ws_frames if f["type"] == "delegate_finished")
    assert finished["tokens_used"] == 15
