"""An aborted task loop must not report a false "maximum tool iterations"
warning.

The abort check at the top of ``_run_task_loop``'s for-loop shares the
loop-exhaustion epilogue, so a session aborted mid-task used to emit the
"reached maximum tool iterations - reply to continue" warning even when the
task had run for a single iteration. The chat loop distinguishes the two
exits; the epilogue warning must now be gated on a real exhaustion.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message, StreamChunk, StopReason
from agent_core.session import ConversationSession
from agent_core.tools.base import ToolContext


class _DrainingSession(ConversationSession):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.events_emitted: list[tuple[str, dict]] = []
        self._drainer: asyncio.Task | None = None

    def start_drainer(self) -> None:
        async def _drain():
            async for evt in self.events():
                self.events_emitted.append((evt.type, dict(evt.data)))
        self._drainer = asyncio.create_task(_drain())

    async def stop_drainer(self) -> None:
        await self.close()
        if self._drainer:
            await self._drainer


class _ToolThenDoneProvider:
    """Would happily run a tool loop; a missing abort gate would let it reach
    the iteration-exhaustion epilogue after the break."""

    model_name = "fake"
    supports_vision = False
    context_window = 100000

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.calls += 1
        yield StreamChunk(
            tool_call_delta={
                "index": 0,
                "id": "call-1",
                "function": {"name": "noop_tool", "arguments": "{}"},
            },
            stop_reason=StopReason.TOOL_USE,
        )


def _make_agent() -> Agent:
    config = AgentConfig(slug="test", description="test", system_prompt="s")
    tool_ctx = MagicMock()
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)
    tool_ctx.cleanup = AsyncMock()
    return Agent(
        config=config,
        credentials={"cfg1": {"api_key": "k"}},
        tier_models={"cfg1": {"provider": "openai", "model": "m"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )


@pytest.mark.asyncio
async def test_aborted_task_loop_emits_no_iteration_limit_warning():
    agent = _make_agent()
    tool = MagicMock()
    tool.execute = AsyncMock(return_value={"ok": True})
    agent._tools["noop_tool"] = tool

    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    # Abort before the loop starts: the iteration-0 check breaks immediately
    # and control lands on the shared epilogue with zero real iterations.
    session.abort()
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )
    messages = [Message(role="user", content="do it")]

    await agent._run_task_loop(
        messages, [], _ToolThenDoneProvider(), session,
        task_id="t1", turn=1, task_tool_ctx=ctx,
    )
    await session.stop_drainer()

    warnings = [data for kind, data in session.events_emitted if kind == "warning"]
    assert not any("maximum tool iterations" in w.get("message", "") for w in warnings)
    # The stream must still close — a stuck typing indicator was the other
    # half of the epilogue's job on every exit path.
    kinds = [kind for kind, _ in session.events_emitted]
    assert "stream_end" in kinds


@pytest.mark.asyncio
async def test_exhausted_task_loop_still_warns(monkeypatch):
    """The gated warning stays for a genuine exhaustion — aborts must not
    silence the real iteration-limit signal."""
    from agent_core import agent as _agent_mod

    monkeypatch.setattr(_agent_mod, "_MAX_TASK_ITERATIONS", 1)

    agent = _make_agent()
    tool = MagicMock()
    tool.execute = AsyncMock(return_value={"ok": True})
    agent._tools["noop_tool"] = tool

    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )
    messages = [Message(role="user", content="do it")]

    await agent._run_task_loop(
        messages, [], _ToolThenDoneProvider(), session,
        task_id="t1", turn=1, task_tool_ctx=ctx,
    )
    await session.stop_drainer()

    warnings = [data for kind, data in session.events_emitted if kind == "warning"]
    assert any("maximum tool iterations" in w.get("message", "") for w in warnings)
