"""What the user — and the next turn — sees of a delegated reply.

Decision 6: the child's answer is a first-class message. It is persisted under
its own ``agent_slug`` and announced with ``conversation_updated``, the same
pair the scheduled-task delivery uses, so the existing "answered by X" badge
renders it and a page reload keeps it.

Attribution also has to survive into the *model's* view of the conversation:
the next turn loads history through ``_load_history``, which prefixes every
assistant reply produced by a different slug with ``[display_name]:``. Without
it a delegating agent reads the child's words as its own prior output and can
argue with itself. The stored row is never prefixed — the marker exists only in
the reconstructed history, and the second test pins that split.
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
from api.routers.agents import _sync_project_agents
from api.services.agent_turn import _load_history
from api.services.delegation import DelegationContext, run_delegated_turn
from api.services.token_vault import encrypt
from api.websocket.handlers import _handle_message
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


@pytest.fixture
def frames(monkeypatch):
    """Record every frame that reaches the connection manager."""
    seen: list[dict] = []

    async def _send(conversation_id: str, data: dict) -> bool:
        seen.append(data)
        return True

    monkeypatch.setattr(manager, "send", _send)
    return seen


@pytest.fixture
def agent_spy(monkeypatch):
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)
            if state["behavior"] is not None:
                await state["behavior"](kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


async def _setup(db, make_project, tag: str):
    """A project whose agent list has been synced, as the UI would have done."""
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

    child_slug = f"{project.slug}--helper"
    (Path(project_fs(project)) / "agents" / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        "display_name: Helper Bot\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    await _sync_project_agents(db, project)
    return project, conv, child_slug


def _ctx(session, project, conv):
    return SimpleNamespace(
        delegation=DelegationContext(
            chain=("visp--parent",), parent_session=session, actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )


async def test_a_delegated_reply_is_its_own_message_and_attributed_next_turn(
    agent_spy, db, make_project, frames
):
    project, conv, child_slug = await _setup(db, make_project, "visp")
    seen: dict = {}
    child_msg_id = str(uuid.uuid4())

    async def _behavior(kwargs):
        session = kwargs["session"]
        content = kwargs["user_message"]
        if content.startswith("CHILD:"):
            await session.emit("stream_delta", delta="child reply")
            await session.emit("stream_end", message_id=child_msg_id, total_tokens=4)
            return
        if not seen.get("delegated"):
            seen["delegated"] = True
            seen["result"] = await run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=child_slug, brief="CHILD: do the thing", reason="no capability",
            )
            await session.emit("stream_delta", delta="parent final")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=2)
            return
        # The next user message: the parent's own history now contains the
        # child's reply, which is where attribution has to hold.
        seen["history"] = kwargs["history"]
        await session.emit("stream_delta", delta="ok")
        await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    agent_spy["behavior"] = _behavior
    await _handle_message(conv.conversation_id, SimpleNamespace(app=app), {"type": "message", "content": "hi"}, "test-user")
    manager.release_turn(conv.conversation_id)
    await _handle_message(conv.conversation_id, SimpleNamespace(app=app), {"type": "message", "content": "next"}, "test-user")
    manager.release_turn(conv.conversation_id)

    # The reply is a real message: its own slug, its own text, ordered after the
    # user's message it was answering.
    rows = (await db.execute(
        select(Message).where(Message.conversation_id == conv.conversation_id)
        .order_by(Message.sequence_num)
    )).scalars().all()
    child_row = next(r for r in rows if r.message_id == child_msg_id)
    assert child_row.role == "assistant"
    assert child_row.agent_slug == child_slug
    assert child_row.content == "child reply"
    first_user = next(r for r in rows if r.role == "user")
    assert child_row.sequence_num > first_user.sequence_num
    # Attribution is a rendering of history, never part of the stored text.
    assert not child_row.content.startswith("[")

    # The client was told to reload, which is what makes the badge appear.
    assert "conversation_updated" in [f.get("type") for f in frames]
    finished = next(f for f in frames if f.get("type") == "delegate_finished")
    assert finished["agent"] == child_slug
    assert finished["agent_name"] == "Helper Bot"
    assert finished["status"] == "ok"

    # And the next turn reads it as the other agent's words, not its own.
    attributed = [
        m for m in seen["history"]
        if m.role == "assistant" and str(m.content).startswith("[Helper Bot]:")
    ]
    assert attributed, [m.content for m in seen["history"]]
    assert "child reply" in str(attributed[0].content)


async def test_history_prefixes_other_agents_only(db, make_project):
    """The marker is applied in history reconstruction, and only across slugs."""
    project = await make_project("histp")
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    for seq, role, content, slug in (
        (1, "user", "hi", None),
        (2, "assistant", "mine", "visp--parent"),
        (3, "assistant", "theirs", "visp--other"),
        (4, "assistant", "", "visp--other"),
        (5, "assistant", "legacy", None),
    ):
        db.add(Message(
            conversation_id=conv.conversation_id, role=role, content=content,
            agent_slug=slug, sequence_num=seq,
        ))
    await db.commit()

    history = await _load_history(
        db, conv.conversation_id, Path("."), turn_agent_slug="visp--parent",
    )
    contents = [m.content for m in history if m.role == "assistant"]

    assert "mine" in contents          # the running agent's own reply
    assert "legacy" in contents        # written before agent_slug existed
    # An unknown slug falls back to the slug itself, which is still enough for
    # the agent to tell the two voices apart.
    assert "[visp--other]:\ntheirs" in contents
    # A tool-only turn has no text to attribute, and an empty marker would be
    # noise the agent has to reason about.
    assert "" in contents
    assert not any(c.startswith("[visp--other]:\n\n") for c in contents)


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
