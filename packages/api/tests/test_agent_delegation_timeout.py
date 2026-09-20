"""The bound that keeps a delegation from running forever.

A delegated turn is awaited inline by the agent that asked for it, so a child
that hangs hangs the whole conversation: the parent's bubble never closes, Stop
is the only way out, and the tokens keep burning. ``run_delegated_turn`` arms an
``asyncio`` timer whose whole job is to set a private cancel event; the nested
turn's abort watcher is the only thing that can act on it, because nothing else
holds a handle on that turn's agent task.

Both halves are invisible from the outside when they break — the child simply
keeps running and the parent waits — so this file drives a real nested turn
through a real timeout. The settings are patched on the cached object rather
than through the environment, because ``get_settings`` is ``lru_cache``d and the
code reads the attribute, not the env var.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.agent import Agent
from agent_core.session import ConversationSession
from api import config as api_config
from api.main import app
from api.models.conversation import Conversation
from api.models.user import UserModelConfig
from api.paths import AGENTS_DIR, WORKFLOWS_DIR
from api.routers.agents import _sync_project_agents
from api.services.delegation import DelegationContext, run_delegated_turn
from api.services.token_vault import encrypt


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
    state: dict = {"behavior": None, "calls": []}

    class _SpyAgent(Agent):
        async def run(self, **kwargs):
            state["calls"].append(kwargs)
            if state["behavior"] is not None:
                await state["behavior"](kwargs)

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return state


def project_fs(project) -> str:
    from api.paths import PROJECTS_ROOT

    return str(PROJECTS_ROOT / project.slug)


def _drained(session: ConversationSession) -> list[tuple[str, dict]]:
    """The events this session emitted — what forward_events would have read."""
    out: list[tuple[str, dict]] = []
    while not session._event_queue.empty():
        event = session._event_queue.get_nowait()
        if event is not None:
            out.append((event.type, event.data))
    return out


async def test_a_child_that_runs_past_its_limit_is_stopped_and_reported(
    agent_spy, db, make_project, monkeypatch
):
    monkeypatch.setattr(api_config.get_settings(), "agent_delegation_timeout_seconds", 0.2)

    project = await make_project("timeoutp")
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

    child_slug = f"{project.slug}--napper"
    agents_dir = Path(project_fs(project)) / "agents"
    (agents_dir / f"{child_slug}.agent.md").write_text(
        "---\n"
        f"slug: {child_slug}\n"
        "display_name: Napper\n"
        "description: test agent\n"
        "tools: [filesystem]\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    await _sync_project_agents(db, project)

    started = {"child": False}
    ran_to_completion = {"child": False}

    async def _behavior(kwargs):
        started["child"] = True
        await kwargs["session"].emit("stream_delta", delta="thinking")
        await asyncio.sleep(30)
        # Reached only if nothing cancelled the child — the timeout's whole
        # point is that this line never runs.
        ran_to_completion["child"] = True

    agent_spy["behavior"] = _behavior

    parent_session = ConversationSession(conv.conversation_id, project.project_id, "test-user")
    context = SimpleNamespace(
        delegation=DelegationContext(
            chain=(f"{project.slug}--parent",),
            parent_session=parent_session,
            actor_user_id="test-user",
        ),
        app=app,
        project_fs_path=project_fs(project),
        conversation_id=conv.conversation_id,
        interactive=True,
    )

    # The outer wait_for is only a test guard: a regression that hangs must fail
    # here rather than stall the suite.
    result = await asyncio.wait_for(
        run_delegated_turn(
            context, agent_slug=child_slug, brief="CHILD: nap", reason="missing tool",
        ),
        timeout=20,
    )

    assert started["child"], "the child never ran"
    assert not ran_to_completion["child"], "the child finished despite blowing its limit"
    assert result["status"] == "timeout", result
    assert result["agent"] == child_slug
    assert result["agent_name"] == "Napper"
    # Whatever the child managed to say before stalling is kept: the parent can
    # still use it, and a bare "timed out" would make it retry from scratch.
    assert result["summary"] == "thinking"
    assert "limit" in result["error"] and "stopped" in result["error"]

    events = _drained(parent_session)
    finished = [data for kind, data in events if kind == "delegate_finished"]
    assert [f["status"] for f in finished] == ["timeout"], events
    assert finished[0]["agent_name"] == "Napper"
    # The client is still told to reload: a timed-out child may have persisted a
    # partial reply before it was stopped.
    assert "conversation_updated" in [kind for kind, _ in events]
