"""Provider error boundaries must never carry the configured api_key.

Provider/SDK exceptions echo what they rejected — h11's "Illegal header
value b'...'" literally IS the credential — and agent.run() sends that string
to the UI (the error event), to the server log (exc_info) and, through the
task path, to the agent_tasks table (task_failed). The scrub happens at the
boundary, in both raw and bytes-repr escaped forms.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message
from agent_core.session import ConversationSession

BAD_KEY = "sk-agent-error-key\n"
ESCAPED = repr(BAD_KEY.encode("utf-8"))[2:-1]


class _LeakyProvider:
    """First stream() call raises an h11-style echo of the key."""

    model_name = "test-model"
    context_window = 128_000
    supports_vision = False
    supports_tool_calls = True

    async def stream(self, *args, **kw):
        raise RuntimeError(f"Illegal header value b'Bearer {ESCAPED}'")
        yield  # pragma: no cover — makes this an async generator


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
        credentials={"cfg1": {"api_key": BAD_KEY}},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )


@pytest.mark.asyncio
async def test_main_loop_error_event_and_log_scrub_the_api_key(caplog):
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    with caplog.at_level(logging.DEBUG):
        await agent._run_loop(
            [Message(role="user", content="hi")], [], _LeakyProvider(), session, "cfg1"
        )
    await session.stop_drainer()

    errors = [d for t, d in session.events_emitted if t == "error"]
    assert errors, "the failed stream must surface an error event"
    msg = errors[0]["message"]
    assert "REDACTED" in msg
    assert BAD_KEY not in msg
    assert ESCAPED not in msg
    # The log gets the formatted traceback instead of exc_info — scrubbed too.
    assert BAD_KEY not in caplog.text
    assert ESCAPED not in caplog.text
    assert "REDACTED" in caplog.text


@pytest.mark.asyncio
async def test_task_loop_runtime_error_scrubs_the_api_key(caplog):
    from agent_core.tools.base import ToolContext

    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as excinfo:
            await agent._run_task_loop(
                [Message(role="user", content="do it")], [], _LeakyProvider(), session,
                task_id="t1", turn=1, task_tool_ctx=ctx,
            )
    await session.stop_drainer()

    # The RuntimeError is persisted via task_failed — scrubbed at the raise.
    msg = str(excinfo.value)
    assert "REDACTED" in msg
    assert BAD_KEY not in msg
    assert ESCAPED not in msg
    assert BAD_KEY not in caplog.text
    assert ESCAPED not in caplog.text
    assert "REDACTED" in caplog.text
