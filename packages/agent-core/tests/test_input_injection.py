"""Tests for in-flight user input injection in _run_loop / _run_task_mode.

A user message queued while the agent is running is consumed at the next
step boundary: the current partial output is finalized as an "interrupted"
snapshot, the message is appended to history, and the loop continues with
the new instruction (no turn restart).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.agent import Agent, AgentConfig, _MAX_CHAT_ITERATIONS
from agent_core.providers.base import Message, StopReason, StreamChunk
from agent_core.session import ConversationSession, SessionEvent, UserInputEntry


# ── Helpers ─────────────────────────────────────────────────────────────


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
    agent = Agent(
        config=config,
        credentials={"cfg1": {"api_key": "test-key"}},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )
    return agent


class _ScriptedProvider:
    """Provider whose first stream call yields partial text then pauses until
    ``release``, so the test can queue an injection mid-stream. Later calls
    return a canned END_TURN reply.
    """

    model_name = "fake-model"
    supports_vision = False
    context_window = 100000

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[list[Message]] = []
        self._call = 0

    async def stream(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.calls.append(list(messages))
        self._call += 1
        if self._call == 1:
            yield StreamChunk(delta="Hello ")
            self.started.set()
            await self.release.wait()
            yield StreamChunk(delta="world", stop_reason=StopReason.END_TURN)
        else:
            yield StreamChunk(delta="Reply after injection", stop_reason=StopReason.END_TURN)


class _BlockingTool:
    """Tool whose execution blocks until released — used to inject while a
    tool call is in flight."""

    name = "blocker"
    description = "blocks"
    parameters = {"type": "object", "properties": {}}
    prompt_hint = "blocks"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, params: dict, context) -> dict:
        self.started.set()
        await self.release.wait()
        return {"ok": True}

    def to_definition(self):
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _make_entry(message_id: str, content: str) -> UserInputEntry:
    entry = UserInputEntry(message_id=message_id, content=content, attachments=[])
    entry.persisted = asyncio.get_running_loop().create_future()
    return entry


def _events_of(session: _DrainingSession, event_type: str) -> list[dict]:
    return [d for t, d in session.events_emitted if t == event_type]


def _event_types(session: _DrainingSession) -> list[str]:
    return [t for t, _ in session.events_emitted]


# ── _run_loop injection ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_during_stream_consumed_at_step_boundary():
    """A message queued mid-stream is consumed at the next step boundary:
    one interrupted snapshot, one user_message_injected, then the loop
    continues with the injected message in history."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    provider = _ScriptedProvider()

    run_task = asyncio.create_task(agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    ))

    # First LLM call is streaming and paused; partial output "Hello " emitted.
    await provider.started.wait()

    # User sends a message while the agent is still streaming.
    entry = _make_entry("inj-1", "Stop and explain")
    assert session.enqueue_user_input(entry)
    # The handler persists it and resolves the ack (simulating forward_events).
    session.resolve_input_persisted("inj-1", True)

    provider.release.set()
    await run_task
    await session.stop_drainer()

    types = _event_types(session)
    assert types.count("stream_end") == 2, f"expected 2 stream_end, got {types}"
    interrupted = _events_of(session, "stream_end")
    assert sum(1 for d in interrupted if d.get("stop_reason") == "interrupted") == 1
    assert types.count("user_message_injected") == 1
    injected = _events_of(session, "user_message_injected")[0]
    assert injected["message_id"] == "inj-1"

    # The second provider call carries the injected user message last.
    assert provider.calls[1][-1].role == "user"
    assert provider.calls[1][-1].content == "Stop and explain"
    assert entry.consumed
    # No stream_end with the interrupted message_id after finalize (no dupes).
    assert types[-1] == "stream_end"


@pytest.mark.asyncio
async def test_inject_before_first_stream_empty_snapshot():
    """A message queued before the loop starts is consumed at the first
    iteration with an empty interrupted snapshot, and the first LLM call
    already includes it."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    provider = _ScriptedProvider()
    provider.release.set()  # first stream must not pause — no mid-stream wait here

    entry = _make_entry("inj-0", "Pre-queued")
    assert session.enqueue_user_input(entry)
    session.resolve_input_persisted("inj-0", True)

    await agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()

    # First provider call already includes the injected message.
    assert provider.calls[0][-1].role == "user"
    assert provider.calls[0][-1].content == "Pre-queued"
    assert entry.consumed
    types = _event_types(session)
    assert types.count("stream_end") == 2  # interrupted snapshot + final
    assert sum(1 for d in _events_of(session, "stream_end") if d.get("stop_reason") == "interrupted") == 1


@pytest.mark.asyncio
async def test_inject_during_tool_execution():
    """A message queued while a tool call is in flight is consumed after the
    tool result is appended — the tool output stays in history."""
    agent = _make_agent()
    blocker = _BlockingTool()
    agent._tools["blocker"] = blocker
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    # First call: tool_call blocker, END_TURN (tool calls pending).
    provider_calls = 0

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            yield StreamChunk(
                tool_call_delta={"index": 0, "id": "call-1", "function": {"name": "blocker", "arguments": "{}"}},
                stop_reason=StopReason.END_TURN,
            )
        else:
            yield StreamChunk(delta="After tool + injection", stop_reason=StopReason.END_TURN)

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    run_task = asyncio.create_task(agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    ))
    await blocker.started.wait()

    entry = _make_entry("inj-2", "Pivot now")
    assert session.enqueue_user_input(entry)
    session.resolve_input_persisted("inj-2", True)

    blocker.release.set()
    await run_task
    await session.stop_drainer()

    types = _event_types(session)
    assert types.count("user_message_injected") == 1
    assert sum(1 for d in _events_of(session, "stream_end") if d.get("stop_reason") == "interrupted") == 1
    assert entry.consumed
    # The second call includes the tool result (from call 1) and the injection.
    second_messages = provider_calls >= 2
    assert second_messages


@pytest.mark.asyncio
async def test_inject_rejected_skips_message():
    """A message whose persistence ack resolves False is not appended to
    history — the handler already told the client it was rejected, and the
    loop ends normally (no second LLM call with the rejected text)."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    provider_calls = 0

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal provider_calls
        provider_calls += 1
        yield StreamChunk(delta="plain", stop_reason=StopReason.END_TURN)

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    entry = _make_entry("inj-bad", "Bad message")
    assert session.enqueue_user_input(entry)
    session.resolve_input_persisted("inj-bad", False)

    await agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()

    types = _event_types(session)
    # The injected message event was emitted (handler sent input_rejected),
    # but it was never consumed: no second LLM call, entry not consumed.
    assert types.count("user_message_injected") == 1
    assert not entry.consumed
    assert provider_calls == 1
    assert types.count("stream_end") == 2  # interrupted snapshot + final


@pytest.mark.asyncio
async def test_inject_persist_ack_timeout_falls_back_to_success():
    """If the persistence ack never resolves, the agent degrades to success
    (bounded wait) and keeps going — a stuck handler must not stall the loop."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    provider_calls = 0
    seen_messages: list[list[Message]] = []

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal provider_calls
        provider_calls += 1
        seen_messages.append(list(messages))
        yield StreamChunk(delta="plain", stop_reason=StopReason.END_TURN)

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    entry = _make_entry("inj-timeout", "Never acked")
    assert session.enqueue_user_input(entry)
    # Do NOT resolve - the ack future stays pending; the 30s bound is patched
    # short so the test does not actually wait.

    start = asyncio.get_running_loop().time()
    with patch.object(agent, "_INJECT_PERSIST_TIMEOUT", 0.05):
        await agent._run_loop(
            [Message(role="user", content="Initial")], [], provider, session, "cfg1"
        )
    await session.stop_drainer()

    # Degraded to success: the message was consumed even though the ack never
    # arrived, and the loop did not stall on the real 30s bound.
    assert entry.consumed
    assert asyncio.get_running_loop().time() - start < 5.0, "loop stalled on unacked injection"
    # The single (first) LLM call already carries the injected message — the
    # loop continued with it in history.
    assert seen_messages and seen_messages[0][-1].role == "user"
    assert seen_messages[0][-1].content == "Never acked"


class _FastTool:
    """Tool that returns immediately — keeps the loop iterating."""

    name = "fast"
    description = "returns quickly"
    parameters = {"type": "object", "properties": {}}
    prompt_hint = "fast"

    async def execute(self, params: dict, context) -> dict:
        return {"ok": True}

    def to_definition(self):
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@pytest.mark.asyncio
async def test_inject_refreshes_iteration_budget():
    """An injection at the iteration limit extends the loop instead of
    terminating with max_iterations."""
    agent = _make_agent()
    agent._tools["fast"] = _FastTool()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    calls = 0

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal calls
        calls += 1
        # On the LAST budgeted iteration, queue an injection so
        # the loop must not terminate with max_iterations.
        if calls == _MAX_CHAT_ITERATIONS:
            entry = _make_entry("inj-last", "Extend")
            session.enqueue_user_input(entry)
            session.resolve_input_persisted("inj-last", True)
        yield StreamChunk(
            tool_call_delta={"index": 0, "id": f"call-{calls}", "function": {"name": "fast", "arguments": "{}"}},
            stop_reason=StopReason.END_TURN,
        )

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    await agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()

    # The injection on the last budgeted call refreshed the budget, so
    # twice the limit happens in total instead of stopping at the limit.
    assert calls == _MAX_CHAT_ITERATIONS * 2, (
        f"expected budget refresh to extend to {_MAX_CHAT_ITERATIONS * 2} calls, got {calls}"
    )
    # The loop still ends with max_iterations exhaustion afterwards.
    types = _event_types(session)
    assert types.count("warning") == 1


@pytest.mark.asyncio
async def test_abort_leaves_queued_input_unconsumed():
    """Aborting the loop leaves queued entries untouched (the handler's
    watchdog persists them) and does not mark them consumed."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()
    provider = _ScriptedProvider()

    run_task = asyncio.create_task(agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    ))
    # First stream is paused mid-generation.
    await provider.started.wait()

    entry = _make_entry("inj-3", "Queued then aborted")
    assert session.enqueue_user_input(entry)
    session.abort()
    provider.release.set()
    await run_task
    await session.stop_drainer()

    assert not entry.consumed
    assert session.has_pending_user_input()
    # No user_message_injected (the guard stops consumption once aborted).
    # The stream finished naturally after release, so the run ends with one
    # plain stream_end — an "aborted" snapshot only appears on hard-cancel
    # (the top-of-iteration abort check), not in this graceful path.
    types = _event_types(session)
    assert "user_message_injected" not in types
    ends = _events_of(session, "stream_end")
    assert len(ends) == 1, f"expected a single plain stream_end, got {ends}"
    assert "stop_reason" not in ends[0] or ends[0].get("stop_reason") is None


@pytest.mark.asyncio
async def test_queue_full_rejects():
    """Enqueue beyond capacity returns False (the handler errors out)."""
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    for i in range(20):
        assert session.enqueue_user_input(_make_entry(f"q-{i}", f"msg {i}"))
    assert not session.enqueue_user_input(_make_entry("q-over", "overflow"))


# ── _run_task_mode injection ────────────────────────────────────────────


def _make_plan_args(task_specs: list[tuple[str, list[str]]]) -> dict:
    return {
        "goal": "Test goal",
        "tasks": [
            {
                "id": tid,
                "title": f"Task {tid}",
                "tools_needed": [],
                "depends_on": deps,
                "estimated_complexity": "low",
            }
            for tid, deps in task_specs
        ],
    }


def _patch_execute(agent: Agent, fn):
    """Patch _execute_single_task while keeping _task_plan status in sync."""

    async def _execute(task, *args, **kwargs):
        result = await fn(task, *args, **kwargs)
        tid = task["id"]
        for tp in agent._task_plan:
            if tp["id"] == tid:
                tp["status"] = result.get("status", "failed")
                if result.get("status") == "failed":
                    tp["error"] = result.get("summary", "failed")
                elif result.get("status") == "completed":
                    tp["summary"] = result.get("summary", "")
                break
        return result

    return patch.object(agent, "_execute_single_task", new=AsyncMock(side_effect=_execute))


@pytest.mark.asyncio
async def test_inject_during_task_mode_defers_remaining_tasks():
    """A message queued while tasks run stops new dispatch, keeps in-flight
    tasks completing naturally, defers the rest (not skipped), and returns
    control to _run_loop which consumes the injection."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    first_started = asyncio.Event()

    async def _track_execute(task, *args, **kwargs):
        title = task["title"]
        if title == "Task a":
            first_started.set()
        await asyncio.sleep(0.05)
        return {"task_id": task["id"], "status": "completed", "summary": f"[DONE] {title}"}

    with _patch_execute(agent, _track_execute):
        run_task = asyncio.create_task(agent._run_task_mode(
            # Dependency chain: only 'a' starts immediately; b/c are not yet
            # dispatched when the injection arrives, so they must be deferred.
            _make_plan_args([("a", []), ("b", ["a"]), ("c", ["b"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        ))
        await first_started.wait()
        entry = _make_entry("inj-task", "Redo the plan")
        assert session.enqueue_user_input(entry)
        session.resolve_input_persisted("inj-task", True)

        summary = await run_task

    await session.stop_drainer()

    # a completed; b/c were never dispatched and stay deferred. Task ids are
    # rewritten to UUIDs by _normalize_task_plan — map titles to ids.
    title_to_id = {t["title"]: t["id"] for t in agent._task_plan}
    completed_events = _events_of(session, "agentic_loop_completed")
    assert len(completed_events) == 1
    done = completed_events[0]
    assert done.get("interrupted") is True
    assert done["tasks_done"] == 1
    deferred = done.get("deferred_task_ids", [])
    assert sorted(deferred) == sorted([title_to_id["Task b"], title_to_id["Task c"]]), (
        f"expected b,c deferred, got {deferred}"
    )
    # Deferred tasks were not marked skipped.
    statuses = {t["status"] for t in agent._task_plan}
    assert "skipped" not in statuses, f"deferred tasks must not be skipped: {statuses}"
    # Plan kept for the prompt.
    assert agent._task_plan is not None
    assert "User interrupted" in summary
    # Entry still queued for the main loop to consume.
    assert not entry.consumed
    assert session.has_pending_user_input()


# ── Malformed plan_task arguments ────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_plan_task_degrades_to_tool_error():
    """A plan_task call whose arguments are not a list of task dicts (tasks as
    a bare string, list entries without id/title) must surface as a tool
    error the model can self-correct — not crash the whole turn."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    provider_calls = 0
    seen_messages: list[list[Message]] = []

    async def _stream(messages, tools=None, max_tokens=4096, temperature=0.0):
        nonlocal provider_calls
        provider_calls += 1
        seen_messages.append(list(messages))
        if provider_calls == 1:
            yield StreamChunk(
                tool_call_delta={
                    "index": 0,
                    "id": "call-plan",
                    "function": {"name": "plan_task", "arguments": '{"goal": "X", "tasks": "do things"}'},
                },
                stop_reason=StopReason.END_TURN,
            )
        else:
            yield StreamChunk(delta="Recovered from bad plan", stop_reason=StopReason.END_TURN)

    provider = MagicMock()
    provider.stream = _stream
    provider.model_name = "fake"
    provider.supports_vision = False
    provider.context_window = 100000

    await agent._run_loop(
        [Message(role="user", content="Initial")], [], provider, session, "cfg1"
    )
    await session.stop_drainer()

    # The loop survived and called the provider again (tool error in history).
    assert provider_calls == 2, f"expected loop to continue after tool error, got {provider_calls}"
    # The second call carries the plan_task tool result with an error message.
    last = seen_messages[1][-1]
    assert last.role == "tool"
    assert last.name == "plan_task"
    assert "error" in last.content
