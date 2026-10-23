"""Non-object tool arguments must fail as a tool error, not kill the turn.

_parse_tool_arguments used to accept any JSON value — a model emitting
`null`, an array, or a bare string as the arguments payload got it through,
the tool execute try/except swallowed the shape error into an error result,
and then the knowledge_rw/memory_rw post-execution hooks called
args.get(...) outside that guard: AttributeError escaped _run_loop and the
whole turn died instead of handing the model a correction it can act on.
"""
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

    def start_drainer(self) -> None:
        async def _drain():
            async for evt in self.events():
                self.events_emitted.append((evt.type, dict(evt.data)))
        self._drainer = asyncio.create_task(_drain())

    async def stop_drainer(self) -> None:
        await self.close()
        if self._drainer:
            await self._drainer


class _ScriptedProvider:
    """One tool call whose arguments come from the test, then END_TURN."""

    model_name = "fake"
    supports_vision = False
    context_window = 100000

    def __init__(self, tool_name: str, arguments: str) -> None:
        self.calls = 0
        self._tool_name = tool_name
        self._arguments = arguments

    async def stream(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "t1",
                    "function": {"name": self._tool_name, "arguments": self._arguments},
                },
                stop_reason=StopReason.TOOL_USE,
            )
        else:
            yield StreamChunk(delta="done", stop_reason=StopReason.END_TURN)


def _make_agent() -> Agent:
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
    # Truthy project context arms the knowledge_rw post-execution hook —
    # the args.get(...) that used to crash on a non-dict payload.
    agent._project_context = MagicMock()
    return agent


async def _run_turn(agent: Agent, provider: _ScriptedProvider) -> list[tuple[str, dict]]:
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    await agent._run_loop(
        [Message(role="user", content="hi")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()
    return session.events_emitted


async def _run_task_turn(agent: Agent, provider: _ScriptedProvider) -> list[tuple[str, dict]]:
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    await agent._run_task_loop(
        [Message(role="user", content="hi")], [], provider, session,
        task_id="task-1", turn=1, task_tool_ctx=agent._tool_ctx,
    )
    await session.stop_drainer()
    return session.events_emitted


@pytest.mark.asyncio
async def test_chat_loop_array_arguments_become_tool_error():
    """knowledge_rw with a JSON-array payload: malformed-args tool error, turn lives."""
    events = await _run_turn(_make_agent(), _ScriptedProvider("knowledge_rw", "[1, 2]"))
    types = [t for t, _ in events]
    assert "error" not in types
    assert types.count("stream_end") == 1
    ends = [d for t, d in events if t == "tool_call_end"]
    assert ends and "Malformed tool arguments" in str(ends[0].get("output"))


@pytest.mark.asyncio
async def test_chat_loop_null_arguments_become_tool_error():
    """A bare `null` payload parses to None — same crash path, same guard."""
    events = await _run_turn(_make_agent(), _ScriptedProvider("memory_rw", "null"))
    types = [t for t, _ in events]
    assert "error" not in types
    assert types.count("stream_end") == 1


@pytest.mark.asyncio
async def test_task_loop_non_object_arguments_do_not_fail_subtask():
    """The subtask loop shares the hook: a non-dict used to raise AttributeError
    out of _run_task_loop and surface as 'Execution error: ... has no attribute
    get' instead of a tool error the model can correct."""
    events = await _run_task_turn(_make_agent(), _ScriptedProvider("memory_rw", '"str"'))
    types = [t for t, _ in events]
    assert "error" not in types
    assert types.count("stream_end") == 1
    ends = [d for t, d in events if t == "tool_call_end"]
    assert ends and "Malformed tool arguments" in str(ends[0].get("output"))
