"""Recursion bounds, the allowlist, the kill switch, and the concurrency gate.

Delegation is recursive, so it needs a floor under it: a depth cap, a cycle
refusal (an agent must not hand the subtask back to itself or to anyone already
holding it), an optional per-agent allowlist, and a global kill switch. Every
one of those refusals must fire *before* a turn starts — a refusal that still
consumed a model call would be worse than no cap at all.

Because a single turn can fan out (``plan_task`` runs its subtasks in
parallel), two delegations in one conversation must not interleave their message
inserts either — hence the conversation's delegation gate, which the outermost
delegation holds for the whole of its child's turn. That gate must NOT be
re-entered by a descendant: the ancestor holds it until the child returns, so a
grandchild acquiring it would deadlock. ``holds_gate`` is what makes a depth-2
chain terminate (F12), so the last test runs under ``wait_for``: a re-entered
gate hangs, it does not fail.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.agent import Agent
from api.config import get_settings
from api.main import app
from api.models.conversation import Conversation
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
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


@pytest.fixture
def agent_spy(monkeypatch):
    """Replace Agent.run with a dispatcher keyed on the user message.

    ``run_turn`` imports ``Agent`` inside the function body, so patching the
    attribute on ``agent_core.agent`` is what a running turn actually picks up.
    """
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)
            if state["behavior"] is not None:
                await state["behavior"](kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


def _write_agent(project, slug: str) -> None:
    (Path(project_fs(project)) / "agents" / f"{slug}.agent.md").write_text(
        "---\n"
        f"slug: {slug}\n"
        f"display_name: {slug}\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )


def _ctx(session, project, conv, *, chain=("parent",), holds_gate=False, policy=()):
    return SimpleNamespace(
        delegation=DelegationContext(
            chain=chain, parent_session=session, actor_user_id="test-user",
            holds_gate=holds_gate, policy=policy,
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )


async def _project_and_conversation(db, make_project, tag: str):
    project = await make_project(tag)
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return project, conv


async def _model_config(db) -> None:
    db.add(UserModelConfig(
        user_id="test-user", provider="openai", model_id="gpt-5.6-luna",
        encrypted_key=encrypt("sk-test-key-1234", "test-user"), key_hint="...1234",
        url_mode="base_url", complexity_tier=None, sort_order=0,
    ))
    await db.commit()


@pytest.fixture
def parent_session():
    """Stand-in for the top-level session: refusals never reach it."""
    class _S:
        async def emit(self, *a, **kw):
            pass

    return _S()


async def _refuse(agent_spy, parent_session, project, conv, slug, **kw):
    """Call run_delegated_turn and require a refusal that started no turn."""
    agent_spy["behavior"] = None
    result = await run_delegated_turn(
        _ctx(parent_session, project, conv, **kw),
        agent_slug=slug, brief="brief", reason="r",
    )
    assert agent_spy["calls"] == [], "a refused delegation still ran a turn"
    return result


async def test_depth_cap_refuses_and_starts_nothing(
    agent_spy, db, make_project, parent_session
):
    project, conv = await _project_and_conversation(db, make_project, "depthp")
    _write_agent(project, "depthp--helper")

    # A three-entry chain means the caller already runs at depth 2 — the default
    # cap — so the child it is asking for would be a third level.
    result = await _refuse(
        agent_spy, parent_session, project, conv, "depthp--helper",
        chain=("depthp--root", "depthp--mid", "depthp--leaf"),
    )
    assert result["status"] == "refused"
    assert "depth" in result["error"].lower()


async def test_cycle_is_refused(agent_spy, db, make_project, parent_session):
    project, conv = await _project_and_conversation(db, make_project, "cyclep")
    _write_agent(project, "cyclep--helper")

    # The target is already handling this task further up the chain.
    result = await _refuse(
        agent_spy, parent_session, project, conv, "cyclep--helper",
        chain=("cyclep--helper", "cyclep--mid"),
    )
    assert result["status"] == "refused"
    assert "already handling" in result["error"]


async def test_self_delegation_is_refused(agent_spy, db, make_project, parent_session):
    project, conv = await _project_and_conversation(db, make_project, "selfp")
    _write_agent(project, "selfp--helper")

    result = await _refuse(
        agent_spy, parent_session, project, conv, "selfp--helper",
        chain=("selfp--helper",),
    )
    assert result["status"] == "refused"


async def test_delegates_to_allowlist_refuses_other_agents(
    agent_spy, db, make_project, parent_session
):
    project, conv = await _project_and_conversation(db, make_project, "policyp")
    _write_agent(project, "policyp--allowed")
    _write_agent(project, "policyp--blocked")

    result = await _refuse(
        agent_spy, parent_session, project, conv, "policyp--blocked",
        policy=("policyp--allowed",),
    )
    assert result["status"] == "refused"
    assert "policyp--allowed" in result["error"]


async def test_kill_switch_disables_delegation(
    agent_spy, db, make_project, parent_session, monkeypatch
):
    project, conv = await _project_and_conversation(db, make_project, "killp")
    _write_agent(project, "killp--helper")
    # Settings are lru_cached: patch the live object, not the environment.
    monkeypatch.setattr(get_settings(), "agent_delegation_enabled", False)

    result = await _refuse(
        agent_spy, parent_session, project, conv, "killp--helper",
    )
    assert result["status"] == "refused"
    assert "disabled" in result["error"]


async def test_unknown_agent_is_refused(agent_spy, db, make_project, parent_session):
    project, conv = await _project_and_conversation(db, make_project, "unknownp")

    result = await _refuse(agent_spy, parent_session, project, conv, "unknownp--nope")
    assert result["status"] == "refused"
    assert "list_agents" in result["error"]


async def test_run_without_a_delegation_context_is_refused(db, make_project):
    """agent-core treats the context as opaque: a bare ToolContext cannot delegate."""
    result = await run_delegated_turn(
        SimpleNamespace(delegation=None), agent_slug="x", brief="b", reason="r",
    )
    assert result["status"] == "refused"


async def test_parallel_delegations_are_serialized_by_the_gate(
    agent_spy, db, make_project
):
    """Two delegations from one turn run one after the other, never interleaved."""
    project, conv = await _project_and_conversation(db, make_project, "gatep")
    slug_a, slug_b = "gatep--a", "gatep--b"
    _write_agent(project, slug_a)
    _write_agent(project, slug_b)
    await _model_config(db)

    timeline: list[str] = []
    seen: dict = {}

    async def _behavior(kwargs):
        """One body for every turn; a brief that starts with CHILD: is a child."""
        session = kwargs["session"]
        content = kwargs["user_message"]
        if content.startswith("CHILD:"):
            # The child's user_message is the brief plus the delegation system
            # note, so take the first line only.
            tag = content.split(":")[1].split("\n")[0].strip()
            timeline.append(f"{tag}:start")
            # Hold the child open so an interleaving shows up as an out-of-order
            # pair rather than as a race the test usually wins by accident.
            await asyncio.sleep(0.05)
            await session.emit("stream_delta", delta=f"{tag} done")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
            timeline.append(f"{tag}:end")
            return
        seen["results"] = await asyncio.gather(
            run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=slug_a, brief="CHILD: a", reason="r",
            ),
            run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=slug_b, brief="CHILD: b", reason="r",
            ),
        )
        await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    agent_spy["behavior"] = _behavior
    await _drive_turn(conv)

    assert [r["status"] for r in seen["results"]] == ["ok", "ok"]
    assert sorted(r["summary"] for r in seen["results"]) == ["a done", "b done"]
    # Serialized: each child finished before the next one began.
    assert [t.split(":")[1] for t in timeline] == ["start", "end", "start", "end"]
    assert sorted(t.split(":")[0] for t in timeline[0::2]) == ["a", "b"]


async def test_grandchild_does_not_deadlock_on_the_gate(agent_spy, db, make_project):
    """F12: a descendant inherits the held gate instead of re-acquiring it."""
    project, conv = await _project_and_conversation(db, make_project, "grandp")
    child_slug, grand_slug = "grandp--child", "grandp--grand"
    _write_agent(project, child_slug)
    _write_agent(project, grand_slug)
    await _model_config(db)
    seen: dict = {}

    async def _behavior(kwargs):
        session = kwargs["session"]
        content = kwargs["user_message"]
        if content.startswith("GRANDCHILD:"):
            await session.emit("stream_delta", delta="grand done")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
            return
        if content.startswith("CHILD:"):
            # The child delegates again, with the context the real nested turn
            # hands it: the chain ends at the child, and the gate is already held.
            seen["grand"] = await run_delegated_turn(
                _ctx(
                    session, project, conv,
                    chain=("grandp--parent", child_slug), holds_gate=True,
                ),
                agent_slug=grand_slug, brief="GRANDCHILD: go", reason="r",
            )
            await session.emit("stream_delta", delta="child done")
            await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)
            return
        seen["child"] = await asyncio.wait_for(
            run_delegated_turn(
                _ctx(session, project, conv),
                agent_slug=child_slug, brief="CHILD: go", reason="r",
            ),
            timeout=20,  # a re-entered gate hangs here instead of failing
        )
        await session.emit("stream_end", message_id=str(uuid.uuid4()), total_tokens=1)

    agent_spy["behavior"] = _behavior
    await _drive_turn(conv)

    assert seen["grand"]["status"] == "ok"
    assert seen["grand"]["summary"] == "grand done"
    assert seen["child"]["status"] == "ok"
    assert seen["child"]["summary"] == "child done"


async def _drive_turn(conv) -> None:
    """Run one top-level turn over the spy agent's behavior."""
    try:
        await _handle_message(
            conv.conversation_id, SimpleNamespace(app=app),
            {"type": "message", "content": "hi"}, "test-user",
        )
    finally:
        manager.release_turn(conv.conversation_id)


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)
