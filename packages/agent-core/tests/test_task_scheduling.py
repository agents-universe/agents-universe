"""Tests for DAG-aware parallel task scheduling in _run_task_mode.

These tests mock _execute_single_task to verify scheduling behavior
(dispatch order, parallelism, cascade-skip on failure, abort handling)
without needing real LLM providers or tool execution.

NOTE: _normalize_task_plan rewrites task ids to UUIDs, so all tests track
tasks by their stable titles and map titles -> UUIDs via agent._task_plan.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.agent import Agent, AgentConfig
from agent_core.session import ConversationSession, SessionEvent


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_plan_args(task_specs: list[tuple[str, list[str]]]) -> dict:
    """Build plan_task args from (id, depends_on) pairs."""
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


class _DrainingSession(ConversationSession):
    """A session that auto-drains its event queue so emit() never blocks.

    Spawns a background task to consume events as they arrive.
    """

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
    """Build a minimal Agent with mocked internals."""
    config = AgentConfig(
        slug="test",
        description="test",
        system_prompt="You are a test agent.",
    )
    tool_ctx = MagicMock()
    tool_ctx.copy_for_task = MagicMock(return_value=tool_ctx)

    agent = Agent(
        config=config,
        credentials={"cfg1": {"api_key": "test-key"}},
        tier_models={"cfg1": {"provider": "openai", "model": "test-model"}},
        skill_registry=MagicMock(),
        tool_context=tool_ctx,
    )
    return agent


def _title_to_id(agent: Agent) -> dict[str, str]:
    """Map task title -> normalized UUID from the live plan."""
    return {t["title"]: t["id"] for t in agent._task_plan}


def _plan_statuses(agent: Agent) -> dict[str, str]:
    """Map task title -> status from the live plan."""
    return {t["title"]: t["status"] for t in agent._task_plan}


def _patch_execute(agent: Agent, fn):
    """Patch _execute_single_task with an arbitrary async function.

    The wrapper keeps the real implementation's side effect of updating
    ``_task_plan`` status so scheduler assertions on the plan hold.
    """
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


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_independent_tasks_run_in_parallel():
    """Two independent tasks should both be dispatched immediately (no waiting)."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    dispatch_times: dict[str, float] = {}
    start = asyncio.get_event_loop().time()

    async def _track_execute(task, *args, **kwargs):
        title = task["title"]
        dispatch_times[title] = asyncio.get_event_loop().time() - start
        await asyncio.sleep(0.1)
        return {"task_id": task["id"], "status": "completed", "summary": f"[DONE] {task['title']}"}

    with _patch_execute(agent, _track_execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", [])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    assert set(dispatch_times) == {"Task a", "Task b"}, f"Both tasks should run, got {set(dispatch_times)}"
    assert abs(dispatch_times["Task a"] - dispatch_times["Task b"]) < 0.05, (
        f"Tasks not dispatched in parallel: a={dispatch_times['Task a']:.3f}, b={dispatch_times['Task b']:.3f}"
    )


@pytest.mark.asyncio
async def test_dependent_task_waits_for_dependency():
    """Task B depends on A: B must start only after A completes."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    start_times: dict[str, float] = {}
    start = asyncio.get_event_loop().time()

    async def _track_execute(task, *args, **kwargs):
        title = task["title"]
        start_times[title] = asyncio.get_event_loop().time() - start
        await asyncio.sleep(0.1)
        return {"task_id": task["id"], "status": "completed", "summary": f"[DONE] {task['title']}"}

    with _patch_execute(agent, _track_execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", ["a"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    assert start_times["Task a"] < 0.05, f"A should start immediately, got {start_times['Task a']:.3f}"
    assert start_times["Task b"] >= 0.09, f"B should start after A completes, got {start_times['Task b']:.3f}"


@pytest.mark.asyncio
async def test_failed_task_cascades_skip_to_dependents():
    """Task A fails -> B (depends on A) is skipped -> C (depends on B) is also skipped."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    executed: list[str] = []

    async def _track_execute(task, *args, **kwargs):
        title = task["title"]
        executed.append(title)
        await asyncio.sleep(0.01)
        if title == "Task a":
            return {"task_id": task["id"], "status": "failed", "summary": f"[ERROR] {task['title']}"}
        return {"task_id": task["id"], "status": "completed", "summary": f"[DONE] {task['title']}"}

    with _patch_execute(agent, _track_execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", ["a"]), ("c", ["b"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    # Only A should have been executed; B and C should be skipped
    assert executed == ["Task a"], f"Only 'a' should execute, got {executed}"

    # Verify task_skipped events for B and C (cascade-skipped, never executed)
    title_to_id = _title_to_id(agent)
    skipped_events = [(t, d) for t, d in session.events_emitted if t == "task_skipped"]
    skipped_ids = {d.get("task_id") for _, d in skipped_events}
    assert title_to_id["Task b"] in skipped_ids, f"B should be skipped (task_skipped), got ids: {skipped_ids}"
    assert title_to_id["Task c"] in skipped_ids, f"C should be skipped (task_skipped), got ids: {skipped_ids}"
    # Cascade-skip must not emit task_failed — skipped is not a failure
    failed_events = [(t, d) for t, d in session.events_emitted if t == "task_failed"]
    failed_ids = {d.get("task_id") for _, d in failed_events}
    assert title_to_id["Task b"] not in failed_ids, f"B must not be task_failed, got ids: {failed_ids}"
    assert title_to_id["Task c"] not in failed_ids, f"C must not be task_failed, got ids: {failed_ids}"

    # Verify skipped status in the plan
    statuses = _plan_statuses(agent)
    assert statuses["Task a"] == "failed"
    assert statuses["Task b"] == "skipped"
    assert statuses["Task c"] == "skipped"


@pytest.mark.asyncio
async def test_diamond_dependency_parallelism():
    """Diamond: A -> {B, C} -> D. B and C should run in parallel after A."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    start_times: dict[str, float] = {}
    start = asyncio.get_event_loop().time()

    async def _track_execute(task, *args, **kwargs):
        title = task["title"]
        start_times[title] = asyncio.get_event_loop().time() - start
        await asyncio.sleep(0.1)
        return {"task_id": task["id"], "status": "completed", "summary": f"[DONE] {task['title']}"}

    with _patch_execute(agent, _track_execute):
        await agent._run_task_mode(
            _make_plan_args([
                ("a", []),
                ("b", ["a"]),
                ("c", ["a"]),
                ("d", ["b", "c"]),
            ]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    # A starts immediately
    assert start_times["Task a"] < 0.05

    # B and C start after A finishes (~0.1s) and should overlap
    assert start_times["Task b"] >= 0.09
    assert start_times["Task c"] >= 0.09
    assert abs(start_times["Task b"] - start_times["Task c"]) < 0.05, (
        f"B and C should run in parallel: b={start_times['Task b']:.3f}, c={start_times['Task c']:.3f}"
    )

    # D starts after both B and C finish (~0.2s)
    assert start_times["Task d"] >= 0.19, f"D should start after B and C complete, got {start_times['Task d']:.3f}"


@pytest.mark.asyncio
async def test_abort_skips_remaining_tasks():
    """Abort during execution: running tasks stop, pending tasks are skipped."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    async def _slow_execute(task, *args, **kwargs):
        # First task aborts the session, then sleeps
        session.abort_event.set()
        await asyncio.sleep(0.05)
        return {"task_id": task["id"], "status": "completed", "summary": "[DONE]"}

    with _patch_execute(agent, _slow_execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", []), ("c", ["a"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    statuses = _plan_statuses(agent)
    # B and C should not be "running" after abort
    assert statuses["Task b"] != "running", f"B should not be running after abort, got {statuses}"
    assert statuses["Task c"] != "running", f"C should not be running after abort, got {statuses}"
    # C was never dispatched (abort blocked it) — it must emit task_skipped,
    # not task_failed, so the UI shows a grey skip instead of a red error
    title_to_id = _title_to_id(agent)
    skipped_events = [(t, d) for t, d in session.events_emitted if t == "task_skipped"]
    skipped_c = [d for _, d in skipped_events if d.get("task_id") == title_to_id["Task c"]]
    assert skipped_c, f"C should emit task_skipped after abort, got events: {skipped_events}"
    assert skipped_c[0].get("error") == "Aborted", f"C's skip reason should be Aborted, got: {skipped_c}"
    failed_events = [d for t, d in session.events_emitted if t == "task_failed"]
    assert title_to_id["Task c"] not in {d.get("task_id") for d in failed_events}, (
        f"C must not be task_failed after abort, got: {failed_events}"
    )


@pytest.mark.asyncio
async def test_empty_plan_returns_immediately():
    """An empty task plan should emit agentic_loop_completed and return."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    with _patch_execute(agent, AsyncMock()) as mock_exec:
        result = await agent._run_task_mode(
            {"goal": "Empty", "tasks": []},
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    assert mock_exec.call_count == 0
    loop_events = [(t, d) for t, d in session.events_emitted if t == "agentic_loop_completed"]
    assert len(loop_events) == 1
    assert loop_events[0][1]["tasks_done"] == 0


@pytest.mark.asyncio
async def test_single_chain_is_sequential():
    """A linear chain A->B->C should execute strictly in order."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    execution_order: list[str] = []

    async def _track_execute(task, *args, **kwargs):
        execution_order.append(task["title"])
        await asyncio.sleep(0.01)
        return {"task_id": task["id"], "status": "completed", "summary": "[DONE]"}

    with _patch_execute(agent, _track_execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", ["a"]), ("c", ["b"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    assert execution_order == ["Task a", "Task b", "Task c"], (
        f"Expected sequential execution, got {execution_order}"
    )


@pytest.mark.asyncio
async def test_task_plan_created_includes_depends_on():
    """The task_plan_created event should include depends_on in the task data."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    async def _noop(task, *args, **kwargs):
        return {"task_id": task["id"], "status": "completed", "summary": "[DONE]"}

    with _patch_execute(agent, _noop):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", ["a"])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    plan_events = [(t, d) for t, d in session.events_emitted if t == "task_plan_created"]
    assert len(plan_events) == 1
    tasks_data = plan_events[0][1]["tasks"]
    task_b = next(t for t in tasks_data if t["title"] == "Task b")
    assert task_b.get("depends_on"), f"Task b should have depends_on, got: {task_b}"
    assert len(task_b["depends_on"]) == 1, f"Task b should depend on 1 task, got: {task_b['depends_on']}"
    # The depends_on reference should point at the normalized id of task a
    title_to_id = _title_to_id(agent)
    assert task_b["depends_on"][0] == title_to_id["Task a"], (
        f"depends_on should reference normalized id of A, got: {task_b['depends_on']}"
    )


@pytest.mark.asyncio
async def test_partial_failure_independent_task_continues():
    """If A fails but B is independent, B should still complete."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    async def _execute(task, *args, **kwargs):
        await asyncio.sleep(0.01)
        if task["title"] == "Task a":
            return {"task_id": task["id"], "status": "failed", "summary": "[ERROR]"}
        return {"task_id": task["id"], "status": "completed", "summary": "[DONE]"}

    with _patch_execute(agent, _execute):
        await agent._run_task_mode(
            _make_plan_args([("a", []), ("b", [])]),
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    statuses = _plan_statuses(agent)
    assert statuses["Task a"] == "failed"
    assert statuses["Task b"] == "completed", (
        f"B should complete even though A failed, got {statuses['Task b']}"
    )


@pytest.mark.asyncio
async def test_inflight_task_aborted_emits_task_skipped():
    """Abort firing mid-task: the in-flight task emits task_skipped (not
    completed/failed) and reports status 'skipped' — the UI must not show a
    red error for work that was interrupted, not failed."""
    agent = _make_agent()
    session = _DrainingSession(conversation_id="c1", project_id="p1", user_id="u1")
    session.start_drainer()

    task = {
        "id": "task-1",
        "title": "Task a",
        "tools_needed": [],
        "depends_on": [],
        "estimated_complexity": "low",
    }
    agent._task_plan = [dict(task)]

    async def _run_loop(*args, **kwargs):
        session.abort_event.set()
        return "partial work output"

    with patch.object(agent, "_run_task_loop", new=AsyncMock(side_effect=_run_loop)):
        result = await agent._execute_single_task(
            task,
            provider=MagicMock(),
            session=session,
            messages=[],
            tool_defs=[],
            config_id="cfg1",
            plan_tool_id="call_plan",
        )

    await session.stop_drainer()

    assert result["status"] == "skipped", f"Expected skipped, got {result}"
    skipped = [d for t, d in session.events_emitted if t == "task_skipped"]
    assert skipped and skipped[0]["task_id"] == "task-1", f"Expected task_skipped for task-1, got {skipped}"
    assert skipped[0]["error"] == "Aborted", f"Skip reason should be Aborted, got: {skipped}"
    emitted = [t for t, _ in session.events_emitted]
    assert "task_completed" not in emitted, f"Aborted task must not emit task_completed: {emitted}"
    assert "task_failed" not in emitted, f"Aborted task must not emit task_failed: {emitted}"


# ---------------------------------------------------------------------------
# ConversationSession.close() — queue-full drain keeps the stream content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_on_full_queue_keeps_stream_deltas_and_end():
    """When the event queue is full at close(), the drain must preserve the
    stream content (stream_delta + the final stream_end) so the handler still
    persists the assistant message with its text — only UI-only events are
    dropped. The sentinel must still fit so forward_events() can exit."""
    session = ConversationSession(conversation_id="c", project_id="p", user_id="u")
    queue = session._event_queue
    # Fill the queue: UI-only events first, then the message stream.
    for i in range(queue.maxsize - 3):
        queue.put_nowait(SessionEvent(type="token_update", data={"i": i}))
    queue.put_nowait(SessionEvent(type="stream_delta", data={"delta": "hello "}))
    queue.put_nowait(SessionEvent(type="stream_delta", data={"delta": "world"}))
    queue.put_nowait(SessionEvent(type="stream_end", data={"message_id": "m1"}))
    assert queue.full()

    await session.close()

    drained: list[SessionEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())

    assert drained[-1] is None  # sentinel last — consumer can exit
    types = [e.type for e in drained[:-1]]
    assert types == ["stream_delta", "stream_delta", "stream_end"]
    # The message text survives in order.
    text = "".join(e.data["delta"] for e in drained[:-1] if e.type == "stream_delta")
    assert text == "hello world"


@pytest.mark.asyncio
async def test_close_keeps_content_when_queue_holds_only_stream_events():
    """Worst case: every slot is a stream event — the tail (maxsize-1 events)
    is kept so the sentinel still fits, and the final stream_end survives."""
    session = ConversationSession(conversation_id="c", project_id="p", user_id="u")
    queue = session._event_queue
    for i in range(queue.maxsize - 1):
        queue.put_nowait(SessionEvent(type="stream_delta", data={"delta": f"d{i}"}))
    queue.put_nowait(SessionEvent(type="stream_end", data={"message_id": "m1"}))
    assert queue.full()

    await session.close()

    drained: list[SessionEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is None
    assert drained[-2].type == "stream_end"
    assert all(e.type == "stream_delta" for e in drained[:-2])


@pytest.mark.asyncio
async def test_close_on_full_queue_keeps_terminal_task_events():
    """Terminal task events (task_completed/task_skipped/task_failed) are the
    only source of a task row's final status — the queue-full drain must keep
    them alongside the stream content, or completed work would be swept to
    failed by the handler's stale-task reconcile."""
    session = ConversationSession(conversation_id="c", project_id="p", user_id="u")
    queue = session._event_queue
    # Fill the queue: UI-only events first, then terminal task + stream tail.
    for i in range(queue.maxsize - 3):
        queue.put_nowait(SessionEvent(type="token_update", data={"i": i}))
    queue.put_nowait(SessionEvent(type="task_completed", data={"task_id": "t1", "summary": "done"}))
    queue.put_nowait(SessionEvent(type="stream_delta", data={"delta": "hello "}))
    queue.put_nowait(SessionEvent(type="stream_end", data={"message_id": "m1"}))
    assert queue.full()

    await session.close()

    drained: list[SessionEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is None  # sentinel last — consumer can exit
    types = [e.type for e in drained[:-1]]
    assert types == ["task_completed", "stream_delta", "stream_end"], (
        f"Terminal task events must survive the queue-full drain, got {types}"
    )


@pytest.mark.asyncio
async def test_close_on_full_queue_keeps_task_skipped_event():
    """task_skipped is likewise a terminal task event that must survive."""
    session = ConversationSession(conversation_id="c", project_id="p", user_id="u")
    queue = session._event_queue
    for i in range(queue.maxsize - 2):
        queue.put_nowait(SessionEvent(type="token_update", data={"i": i}))
    queue.put_nowait(SessionEvent(type="task_skipped", data={"task_id": "t2", "error": "Aborted"}))
    queue.put_nowait(SessionEvent(type="stream_end", data={"message_id": "m1"}))
    assert queue.full()

    await session.close()

    drained: list[SessionEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained[-1] is None
    types = [e.type for e in drained[:-1]]
    assert types == ["task_skipped", "stream_end"], f"task_skipped must survive, got {types}"
