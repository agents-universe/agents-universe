"""Tool results carrying a COUNT under `files`/`images` must not kill the turn.

The deliverable/image emit block sits OUTSIDE the tool try/except: a non-list
value iterated there escapes _run_loop as an unhandled TypeError and fails the
whole turn. repo_graph's too_many_files summary is the concrete trigger —
``{"status": "skipped", "files": <int>, ...}`` — so `repo_graph build` on an
oversized repo used to crash with "'int' object is not iterable" instead of
handing the summary to the model.
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


def _make_agent(result: dict) -> Agent:
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
    tool = MagicMock()
    tool.name = "probe"
    tool.description = "d"
    tool.parameters = {"type": "object", "properties": {}}
    tool.prompt_hint = "d"
    tool.to_definition.return_value = {"name": "probe", "description": "d", "input_schema": {}}

    async def _exec(params, context):
        return result
    tool.execute = _exec
    agent._tools["probe"] = tool
    return agent


async def _run_turn(agent: Agent, provider: _ScriptedProvider) -> list[tuple[str, dict]]:
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    await agent._run_loop(
        [Message(role="user", content="hi")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()
    return session.events_emitted


@pytest.mark.asyncio
async def test_files_count_does_not_crash_the_turn():
    """The repo_graph too_many_files shape (files=<int>) must pass through."""
    skipped = {
        "status": "skipped",
        "reason": "too_many_files",
        "files": 12345,
        "max": 10_000,
        "hint": "Repo has 12345 source files",
    }
    events = await _run_turn(_make_agent(skipped), _ScriptedProvider("probe", "{}"))
    types = [t for t, _ in events]
    assert "error" not in types
    assert types.count("stream_end") == 1  # turn completed, not killed
    assert not [d for t, d in events if t == "file_output"]


@pytest.mark.asyncio
async def test_files_list_still_emits_file_output():
    """Real deliverables (list of dicts with url) keep their emit."""
    result = {"files": [{"url": "/api/media/x.png", "name": "x.png"}, "drop-me"]}
    events = await _run_turn(_make_agent(result), _ScriptedProvider("probe", "{}"))
    emitted = [d for t, d in events if t == "file_output"]
    assert len(emitted) == 1
    assert [f["name"] for f in emitted[0]["files"]] == ["x.png"]


@pytest.mark.asyncio
async def test_images_non_list_does_not_crash_the_turn():
    """Same truthiness trap on the images key (a count would iterate here)."""
    events = await _run_turn(
        _make_agent({"images": 3}), _ScriptedProvider("probe", "{}")
    )
    types = [t for t, _ in events]
    assert "error" not in types
    assert not [d for t, d in events if t == "image_output"]
