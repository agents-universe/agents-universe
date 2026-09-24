"""token_update must carry context occupancy alongside the billing ledger.

The meter fraction is prompt+completion of the LATEST provider request
against the provider window; `used` keeps its lifetime-accumulation
semantics (billing). Both ride the same event."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.providers.base import Message, StopReason, StreamChunk
from agent_core.session import ConversationSession


class _DrainingSession(ConversationSession):
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


class _UsageProvider:
    """Two-iteration provider: tool call then END_TURN, usage on both calls."""

    model_name = "fake"
    supports_vision = False
    context_window = 100000

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                tool_call_delta={"index": 0, "id": "t1", "function": {"name": "shell", "arguments": "{}"}},
                stop_reason=StopReason.TOOL_USE,
            )
            yield StreamChunk(usage={"prompt_tokens": 1000, "completion_tokens": 200})
        else:
            yield StreamChunk(delta="done", stop_reason=StopReason.END_TURN)
            yield StreamChunk(usage={"prompt_tokens": 1500, "completion_tokens": 50})


@pytest.mark.asyncio
async def test_token_update_carries_occupancy_and_billing():
    agent = _make_agent()
    tool = MagicMock()
    tool.name = "shell"
    tool.description = "d"
    tool.parameters = {"type": "object", "properties": {}}
    tool.prompt_hint = "d"
    tool.to_definition.return_value = {"name": "shell", "description": "d", "input_schema": {}}

    async def _exec(params, context):
        return {"ok": True}
    tool.execute = _exec
    agent._tools["shell"] = tool

    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    await agent._run_loop(
        [Message(role="user", content="hi")], [], _UsageProvider(), session, "cfg1"
    )
    await session.stop_drainer()

    updates = [d for t, d in session.events_emitted if t == "token_update"]
    assert len(updates) == 2

    # Occupancy = latest request's prompt+completion, window from the provider.
    assert updates[0]["context_tokens"] == 1200
    assert updates[0]["context_window"] == 100000
    assert updates[1]["context_tokens"] == 1550
    assert updates[1]["context_window"] == 100000

    # Billing ledger still accumulates monotonically across iterations.
    assert updates[0]["used"] == 1200
    assert updates[1]["used"] == 1200 + 1550
    assert updates[0]["budget"] == session.token_budget
