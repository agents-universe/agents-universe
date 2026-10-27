"""context_exceeded event — the only affordance the UI has for "adjust the
model's context size". Anthropic signals overflow via a streamed stop reason;
OpenAI/Gemini/gateways raise a 400/413 exception. Both paths must emit the
same context_exceeded (with the config_id the settings button targets) plus a
context_exceeded stream_end — and must NOT emit the raw-error path."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message, StopReason, StreamChunk
from agent_core.session import ConversationSession

API_KEY = "sk-context-exceeded-key"


class _ContextOverflowError(Exception):
    """openai.APIStatusError shape: status_code + message."""

    status_code = 400

    def __init__(self):
        super().__init__(
            "Error code: 400 - {'error': {'message': \"This model's maximum "
            "context length is 8192 tokens, but your messages resulted in 9214\"}}"
        )


class _ContextOverflowProvider:
    """First stream() call raises an OpenAI-shaped context-overflow 400."""

    model_name = "test-model"
    context_window = 128_000
    supports_vision = False
    supports_tool_calls = True

    async def stream(self, *args, **kw):
        raise _ContextOverflowError()
        yield  # pragma: no cover — makes this an async generator


class _StopReasonProvider:
    """Streams cleanly but declares CONTEXT_EXCEEDED (Anthropic-style)."""

    model_name = "test-model"
    context_window = 128_000
    supports_vision = False
    supports_tool_calls = True

    async def stream(self, *args, **kw):
        yield StreamChunk(stop_reason=StopReason.CONTEXT_EXCEEDED)


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


def _make_agent() -> Agent:
    config = AgentConfig(
        slug="test",
        description="test",
        system_prompt="You are a test agent.",
    )
    tool_ctx = MagicMock()
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)
    tool_ctx.cleanup = AsyncMock()  # _run_loop's finally awaits cleanup()
    return Agent(
        config=config,
        credentials={"cfg1": {"api_key": API_KEY}},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )


async def _run_main_loop(agent: Agent, session: _DrainingSession, provider):
    session.start_drainer()
    await agent._run_loop(
        [Message(role="user", content="hi")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()


@pytest.mark.asyncio
async def test_provider_exception_emits_context_exceeded_not_error(caplog):
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")

    with caplog.at_level(logging.DEBUG):
        await _run_main_loop(agent, session, _ContextOverflowProvider())

    types = [t for t, _ in session.events_emitted]
    ctx = [d for t, d in session.events_emitted if t == "context_exceeded"]
    ends = [d for t, d in session.events_emitted if t == "stream_end"]
    assert ctx, "the overflow 400 must surface a context_exceeded event"
    assert ctx[0].get("config_id") == "cfg1", "the settings button needs the config id"
    assert ends and ends[0].get("stop_reason") == "context_exceeded"
    # The classified branch replaces the generic error path entirely.
    assert "error" not in types
    # The scrubbed provider log still runs on this branch — and never leaks.
    assert "provider.stream() failed" in caplog.text
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_stop_reason_path_carries_config_id():
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")

    await _run_main_loop(agent, session, _StopReasonProvider())

    ctx = [d for t, d in session.events_emitted if t == "context_exceeded"]
    ends = [d for t, d in session.events_emitted if t == "stream_end"]
    assert ctx and ctx[0].get("config_id") == "cfg1"
    assert ends and ends[0].get("stop_reason") == "context_exceeded"


@pytest.mark.asyncio
async def test_task_loop_context_overflow_raises_same_message():
    from agent_core.tools.base import ToolContext

    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )

    with pytest.raises(RuntimeError) as excinfo:
        await agent._run_task_loop(
            [Message(role="user", content="do it")], [], _ContextOverflowProvider(),
            session, task_id="t1", turn=1, task_tool_ctx=ctx,
        )
    await session.stop_drainer()

    # Identical wording to the stop-reason branch — the task card reads the
    # same whichever path caught the overflow.
    assert str(excinfo.value) == "Context window exceeded during task execution"
