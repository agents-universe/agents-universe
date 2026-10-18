"""plan_task inside a subtask must fail loudly, not silently no-op.

Only the chat loop intercepts plan_task and schedules the returned tasks. In
``_run_task_loop`` the model's call reached ``PlannerTool.execute``, which
merely ACKs (``{"accepted": True}``) because it has no scheduler access — the
model then believed it had planned work that would never run.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message, StopReason, StreamChunk
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


class _PlanThenDoneProvider:
    """First call asks for plan_task, second ends the turn."""

    model_name = "fake"
    supports_vision = False
    context_window = 100000

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "call-plan",
                    "function": {
                        "name": "plan_task",
                        "arguments": '{"goal": "x", "tasks": []}',
                    },
                },
                stop_reason=StopReason.TOOL_USE,
            )
        else:
            yield StreamChunk(delta="done", stop_reason=StopReason.END_TURN)


@pytest.mark.asyncio
async def test_plan_task_in_subtask_returns_error_without_executing_tool():
    config = AgentConfig(slug="test", description="test", system_prompt="s")
    tool_ctx = MagicMock()
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)
    tool_ctx.cleanup = AsyncMock()
    agent = Agent(
        config=config,
        credentials={"cfg1": {"api_key": "k"}},
        tier_models={"cfg1": {"provider": "openai", "model": "m"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )
    planner = MagicMock()
    planner.execute = AsyncMock(return_value={"accepted": True})
    agent._tools["plan_task"] = planner

    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    ctx = ToolContext(
        project_id="p1", project_fs_path="/tmp/p1", conversation_id="c1",
        user_id="u1", db_session=None,
    )
    messages = [Message(role="user", content="do it")]

    text = await agent._run_task_loop(
        messages, [], _PlanThenDoneProvider(), session,
        task_id="t1", turn=1, task_tool_ctx=ctx,
    )
    await session.stop_drainer()

    assert text == "done"
    planner.execute.assert_not_awaited()
    tool_messages = [m for m in messages if m.role == "tool"]
    assert tool_messages
    assert "only available at the top level" in tool_messages[-1].content
