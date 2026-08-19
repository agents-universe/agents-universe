"""Tests for auto-route per-task model selection in _execute_single_task.

A tier_map routes each plan task to the config serving its
estimated_complexity (nearest-tier fallback); without a tier_map the session
provider_key is used for every task (existing behavior).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message
from agent_core.session import ConversationSession


class _DrainingSession(ConversationSession):
    """A session that auto-drains its event queue so emit() never blocks."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.events_emitted: list[tuple[str, dict]] = []
        self._drainer: asyncio.Task | None = None

    def start_drainer(self):
        async def _drain():
            async for evt in self.events():
                self.events_emitted.append((evt.type, dict(evt.data)))
        self._drainer = asyncio.create_task(_drain())

    async def stop_drainer(self):
        await self.close()
        if self._drainer:
            await self._drainer


class _FakeProvider:
    def __init__(self, model_name: str):
        self.model_name = model_name

    async def close(self):
        pass


def _make_agent(tier_map: dict[str, str] | None = None) -> Agent:
    config = AgentConfig(slug="test", description="test", system_prompt="You are a test agent.")
    tool_ctx = MagicMock()
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)

    agent = Agent(
        config=config,
        credentials={"cfg-low": {"api_key": "k1"}, "cfg-mid": {"api_key": "k2"}, "cfg-high": {"api_key": "k3"}},
        tier_models={
            "cfg-low": {"provider": "openai", "model": "low-model"},
            "cfg-mid": {"provider": "openai", "model": "mid-model"},
            "cfg-high": {"provider": "openai", "model": "high-model"},
        },
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
        tier_map=tier_map,
    )
    return agent


def _task(complexity: str | None) -> dict:
    return {
        "id": "task-1",
        "title": "Task task-1",
        "tools_needed": [],
        "depends_on": [],
        "estimated_complexity": complexity,
    }


async def _run_task(agent: Agent, session: _DrainingSession, task: dict) -> dict:
    agent._task_plan = [task]
    agent._build_task_messages = lambda messages, pid, title: messages  # type: ignore[method-assign]
    agent._run_task_loop = AsyncMock(return_value="done")  # type: ignore[method-assign]
    result = await agent._execute_single_task(
        task, None, session, [Message(role="user", content="hi")], [], "cfg-mid", "plan-tool-1"
    )
    # Let the background drainer consume the emitted events before assertions.
    await asyncio.sleep(0.01)
    return result


def _provider_used(agent: Agent) -> str:
    # _run_task_loop(messages, tool_defs, provider, session, ...) — provider is
    # the 3rd positional arg.
    return agent._run_task_loop.await_args.args[2].model_name


@pytest.fixture
def fake_provider_factory(monkeypatch):
    def _fake(provider_type: str, merged: dict) -> _FakeProvider:
        return _FakeProvider(merged.get("model", "?"))

    monkeypatch.setattr("agent_core.agent.get_provider", _fake)


@pytest.fixture
async def session():
    s = _DrainingSession(conversation_id="conv-1", project_id="p1", user_id="u1")
    s.start_drainer()
    try:
        yield s
    finally:
        await s.stop_drainer()


# ── routing ─────────────────────────────────────────────────────────────


async def test_task_routed_by_complexity(fake_provider_factory, session):
    agent = _make_agent(tier_map={"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"})
    await _run_task(agent, session, _task("low"))
    assert _provider_used(agent) == "low-model"
    await _run_task(agent, session, _task("mid"))
    assert _provider_used(agent) == "mid-model"
    await _run_task(agent, session, _task("high"))
    assert _provider_used(agent) == "high-model"


async def test_missing_tier_falls_back_nearest(fake_provider_factory, session):
    """mid missing → low (cheaper) wins; high missing → mid."""
    agent = _make_agent(tier_map={"low": "cfg-low", "high": "cfg-high"})
    await _run_task(agent, session, _task("mid"))
    assert _provider_used(agent) == "low-model"

    agent = _make_agent(tier_map={"low": "cfg-low", "mid": "cfg-mid"})
    await _run_task(agent, session, _task("high"))
    assert _provider_used(agent) == "mid-model"


async def test_unknown_complexity_uses_session_provider(fake_provider_factory, session):
    """None/bogus complexity → default chain (mid) = the session provider."""
    agent = _make_agent(tier_map={"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"})
    await _run_task(agent, session, _task(None))
    assert _provider_used(agent) == "mid-model"


async def test_no_tier_map_keeps_session_provider(fake_provider_factory, session):
    """Explicit mode: every task uses the session provider_key regardless."""
    agent = _make_agent(tier_map=None)
    for complexity in ("low", "mid", "high", None):
        await _run_task(agent, session, _task(complexity))
        assert _provider_used(agent) == "mid-model"


async def test_task_started_carries_actual_model(fake_provider_factory, session):
    agent = _make_agent(tier_map={"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"})
    await _run_task(agent, session, _task("high"))
    started = [e for e in session.events_emitted if e[0] == "task_started"]
    assert len(started) == 1
    data = started[0][1]
    assert data["model_tier"] == "cfg-high"
    assert data["actual_model"] == "high-model"


async def test_task_completed_still_emitted(fake_provider_factory, session):
    agent = _make_agent(tier_map={"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"})
    result = await _run_task(agent, session, _task("low"))
    assert result["status"] == "completed"
    assert [e[0] for e in session.events_emitted].count("task_completed") == 1
