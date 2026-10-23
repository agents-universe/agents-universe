"""request_user_selection must wake on abort — a prompt nobody can answer
(UI closed, queue full) must not block the agent for the full timeout.

Also: a prompt that times out or is aborted must emit user_selection_cancelled
so the client can dismiss the dialog it is still showing — otherwise a zombie
prompt stays pinned in the UI and every later user_confirm stacks another
dialog ("再对话它弹出不了了").
"""
import asyncio

import pytest

from agent_core.session import ConversationSession, SessionEvent


async def _collect_events(s):
    """Drain the session event queue into a list until the None sentinel."""
    events = []
    async for ev in s.events():
        events.append(ev)
    return events


async def test_abort_wakes_prompt_immediately():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("p1", "field", "Approve?", timeout=300)
    )
    await asyncio.sleep(0.05)  # let it reach the wait

    s.abort()

    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await asyncio.wait_for(task, timeout=2)
    assert "p1" not in s._pending_prompts


async def test_abort_before_prompt_also_wakes():
    s = ConversationSession("c1", "p1", "u1")
    s.abort()
    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await s.request_user_selection("p2", "field", "Approve?", timeout=300)
    assert "p2" not in s._pending_prompts


async def test_normal_response_path_unchanged():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("p3", "field", "Approve?", timeout=30)
    )
    await asyncio.sleep(0.05)
    assert s.resolve_user_selection("p3", "yes")

    assert await task == "yes"
    assert "p3" not in s._pending_prompts


async def test_timeout_emits_user_selection_cancelled():
    """A timed-out prompt must emit user_selection_cancelled with the prompt_id."""
    s = ConversationSession("c1", "p1", "u1")
    collector = asyncio.create_task(_collect_events(s))

    with pytest.raises(RuntimeError, match="[Tt]imed out"):
        await s.request_user_selection("pt1", "field", "Approve?", timeout=0.1)

    await s.close()
    events = await asyncio.wait_for(collector, timeout=2)

    cancelled = [e for e in events if e.type == "user_selection_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].data["prompt_id"] == "pt1"
    assert cancelled[0].data["reason"] == "timeout"
    assert cancelled[0].data["field_key"] == "field"
    # The original prompt event must precede the cancel event.
    required = [e.type for e in events]
    assert required.index("user_selection_required") < required.index("user_selection_cancelled")


async def test_abort_emits_user_selection_cancelled():
    """An aborted prompt must emit user_selection_cancelled with reason=aborted."""
    s = ConversationSession("c1", "p1", "u1")
    collector = asyncio.create_task(_collect_events(s))

    task = asyncio.create_task(
        s.request_user_selection("pa1", "field", "Approve?", timeout=300)
    )
    await asyncio.sleep(0.05)
    s.abort()

    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await asyncio.wait_for(task, timeout=2)

    await s.close()
    events = await asyncio.wait_for(collector, timeout=2)

    cancelled = [e for e in events if e.type == "user_selection_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].data["prompt_id"] == "pa1"
    assert cancelled[0].data["reason"] == "aborted"


async def test_task_cancellation_emits_user_selection_cancelled():
    """Cancelled mid-question (delegated turn timeout, teardown): the dialog
    must still be dismissed, and the prompt must not leak in the session."""
    s = ConversationSession("c1", "p1", "u1")
    collector = asyncio.create_task(_collect_events(s))

    task = asyncio.create_task(
        s.request_user_selection("pc1", "field", "Approve?", timeout=300)
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    await s.close()
    events = await asyncio.wait_for(collector, timeout=2)

    cancelled = [e for e in events if e.type == "user_selection_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].data["prompt_id"] == "pc1"
    assert cancelled[0].data["reason"] == "cancelled"
    assert "pc1" not in s._pending_prompts
    assert s.pending_prompt_events() == []


async def test_normal_response_emits_no_cancel():
    """A prompt answered by the user must NOT emit user_selection_cancelled."""
    s = ConversationSession("c1", "p1", "u1")
    collector = asyncio.create_task(_collect_events(s))

    task = asyncio.create_task(
        s.request_user_selection("pn1", "field", "Approve?", timeout=30)
    )
    await asyncio.sleep(0.05)
    assert s.resolve_user_selection("pn1", "yes")
    assert await task == "yes"

    await s.close()
    events = await asyncio.wait_for(collector, timeout=2)

    assert not [e for e in events if e.type == "user_selection_cancelled"]


async def test_cancel_during_prompt_emit_unregisters_from_sink():
    """A cancellation landing inside the prompt-event emit must still
    unregister the prompt from its store.

    The emit is the only await before the cleanup finally — with a full event
    queue it parks there for up to the put timeout. For a delegated child the
    store is the parent's session, which outlives the child: a leaked payload
    would replay an unanswerable dialog on every client reconnect, because
    resolve_user_selection never pops it (only the requester's finally does).
    """
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c2", "p1", "u1", prompt_sink=parent)
    # Fill the child's queue so the prompt emit blocks in put().
    for _ in range(child._event_queue.maxsize):
        child._event_queue.put_nowait(SessionEvent(type="stream_delta", data={}))

    task = asyncio.create_task(
        child.request_user_selection("p9", "field", "Approve?", timeout=300)
    )
    await asyncio.sleep(0.05)  # registered on the sink, parked in emit
    assert [p["prompt_id"] for p in parent.pending_prompt_events()] == ["p9"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert parent.pending_prompt_events() == []
    assert "p9" not in parent._pending_prompts
    assert "p9" not in parent._prompt_owners
