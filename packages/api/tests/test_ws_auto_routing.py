"""Tests for the auto-routing branch of the WS message handler.

Drives _handle_message directly (no socket): a real Agent is constructed and
its run() is replaced by a spy, so the assertions target the handler wiring —
sentinel resolution, tier_map construction, classifier routing — rather than
the agent loop itself.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.agent import Agent
from api.main import app
from api.models.conversation import Conversation
from api.models.user import UserModelConfig
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
    """Replace Agent.run with a spy; captures init + run kwargs."""
    captured: dict = {"init_kwargs": None, "run_kwargs": None, "run_count": 0}

    class _SpyAgent(Agent):
        def __init__(self, *args, **kwargs):
            captured["init_kwargs"] = kwargs
            super().__init__(*args, **kwargs)

        async def run(self, **kwargs):
            captured["run_kwargs"] = kwargs
            captured["run_count"] += 1
            # Mirror the real model_selected emission so the assertion covers
            # the event payload, not just the captured kwargs.
            await kwargs["session"].emit(
                "model_selected",
                provider=kwargs.get("provider_override", ""),
                model="spy-model",
                tier=kwargs.get("auto_tier"),
            )

    monkeypatch.setattr("agent_core.agent.Agent", _SpyAgent)
    return captured


@pytest.fixture(autouse=True)
async def _clean_model_configs(db):
    """The suite shares one DB file — drop rows from previous tests."""
    from sqlalchemy import delete

    from api.models.user import UserModelConfig

    await db.execute(delete(UserModelConfig).where(UserModelConfig.user_id == "test-user"))
    await db.commit()


async def _make_conversation(db, make_project):
    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _add_config(db, *, model_id: str, tier: str | None, sort_order: int) -> str:
    row = UserModelConfig(
        user_id="test-user",
        provider="openai",
        model_id=model_id,
        encrypted_key=encrypt("sk-test-key-1234", "test-user"),
        key_hint="...1234",
        url_mode="base_url",
        complexity_tier=tier,
        sort_order=sort_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.config_id


def _ws():
    return SimpleNamespace(app=app)


async def _send(conversation_id: str, msg: dict) -> None:
    await _handle_message(conversation_id, _ws(), msg, "test-user")


# ── auto routing ─────────────────────────────────────────────────────────


async def test_auto_routes_to_classified_tier(agent_spy, db, make_project, monkeypatch):
    conv = await _make_conversation(db, make_project)
    low_cfg = await _add_config(db, model_id="gpt-5.6-luna", tier="low", sort_order=0)
    high_cfg = await _add_config(db, model_id="gpt-5.6-sol", tier="high", sort_order=1)

    calls = []
    async def _classify(*a, **kw):
        calls.append(a)
        return "high"
    monkeypatch.setattr("api.services.complexity.classify_complexity", _classify)

    await _send(conv.conversation_id, {"type": "message", "content": "build a complex thing", "config_id": "auto"})

    assert calls, "classifier must be invoked for auto mode"
    # classify_complexity(db, conversation_id, content, credentials, tier_models, classifier_config_id)
    assert calls[0][5] == low_cfg, "cheapest tiered config classifies"
    assert agent_spy["run_kwargs"]["provider_override"] == high_cfg
    assert agent_spy["run_kwargs"]["auto_tier"] == "high"
    assert agent_spy["init_kwargs"]["tier_map"] == {"low": low_cfg, "high": high_cfg}


async def test_auto_classifier_failure_falls_back_to_default(agent_spy, db, make_project, monkeypatch):
    conv = await _make_conversation(db, make_project)
    low_cfg = await _add_config(db, model_id="gpt-5.6-luna", tier="low", sort_order=0)
    await _add_config(db, model_id="gpt-5.6-sol", tier="high", sort_order=1)

    async def _classify(*a, **kw):
        return None
    monkeypatch.setattr("api.services.complexity.classify_complexity", _classify)

    await _send(conv.conversation_id, {"type": "message", "content": "hi", "config_id": "auto"})

    # Default selection: first config with credentials (sort_order 0).
    assert agent_spy["run_kwargs"]["provider_override"] == low_cfg
    assert agent_spy["run_kwargs"]["auto_tier"] is None


async def test_auto_without_any_tier_skips_classifier(agent_spy, db, make_project, monkeypatch):
    """Tier-less configs make auto behave exactly like the default selection."""
    conv = await _make_conversation(db, make_project)
    plain_cfg = await _add_config(db, model_id="my-model", tier=None, sort_order=0)

    calls = []
    async def _classify(*a, **kw):
        calls.append(1)
        return "low"
    monkeypatch.setattr("api.services.complexity.classify_complexity", _classify)

    await _send(conv.conversation_id, {"type": "message", "content": "hi", "config_id": "auto"})

    assert calls == [], "no tier_map → no classification call"
    assert agent_spy["run_kwargs"]["provider_override"] == plain_cfg
    assert agent_spy["run_kwargs"]["auto_tier"] is None


async def test_explicit_selection_never_gets_tier_map(agent_spy, db, make_project):
    """Explicit mode routes every plan subtask through the session provider."""
    conv = await _make_conversation(db, make_project)
    low_cfg = await _add_config(db, model_id="gpt-5.6-luna", tier="low", sort_order=0)
    high_cfg = await _add_config(db, model_id="gpt-5.6-sol", tier="high", sort_order=1)

    await _send(conv.conversation_id, {"type": "message", "content": "hi", "config_id": high_cfg})

    assert agent_spy["run_kwargs"]["provider_override"] == high_cfg
    assert agent_spy["run_kwargs"]["auto_tier"] is None
    assert agent_spy["init_kwargs"]["tier_map"] is None
    # The tiered config is untouched by the low-tier routing.
    assert agent_spy["init_kwargs"]["credentials"].get(low_cfg)
