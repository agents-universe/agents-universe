"""Prompt routing for a delegated (nested) turn.

A delegated turn runs its own ``ConversationSession`` but is deliberately not
registered with the connection manager — the top-level turn owns that. The WS
handler resolves a user's answer through the conversation's *registered*
session, so a prompt registered on the nested session alone could never be
answered. Nested sessions therefore point at a ``prompt_sink``.
"""
import asyncio

import pytest

from agent_core.session import ConversationSession


async def _collect_events(s):
    events = []
    async for ev in s.events():
        events.append(ev)
    return events


async def test_delegated_prompt_is_answerable_through_the_registered_session():
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)

    task = asyncio.create_task(
        child.request_user_selection("pd1", "field", "Push to prod?", timeout=30)
    )
    await asyncio.sleep(0.05)

    # The answer arrives on the registered session, addressed by prompt_id.
    pending = parent.pending_prompt_events()
    assert [p["prompt_id"] for p in pending] == ["pd1"]
    assert parent.resolve_user_selection("pd1", "yes") is True

    assert await task == "yes"


async def test_resolved_delegated_prompt_leaves_no_trace_on_the_sink():
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)

    task = asyncio.create_task(
        child.request_user_selection("pd2", "field", "Push?", timeout=30)
    )
    await asyncio.sleep(0.05)
    parent.resolve_user_selection("pd2", "yes")
    await task

    assert parent._pending_prompts == {}
    assert parent._pending_prompt_events == {}
    assert parent._prompt_owners == {}
    assert parent.pending_prompt_events() == []
    assert child._pending_prompts == {}


async def test_cancelled_delegated_prompt_leaves_no_trace_on_the_sink():
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)
    collector = asyncio.create_task(_collect_events(child))

    task = asyncio.create_task(
        child.request_user_selection("pd3", "field", "Push?", timeout=300)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await child.close()
    events = await asyncio.wait_for(collector, timeout=2)

    # The dismissal reaches the client through the child's own event stream.
    assert [e.data["reason"] for e in events if e.type == "user_selection_cancelled"] == ["cancelled"]
    assert parent._pending_prompts == {}
    assert parent._pending_prompt_events == {}
    assert parent._prompt_owners == {}


async def test_answering_lifts_the_pause_on_the_asking_session():
    """The ledger that suppresses repeat questions lives on the asking session."""
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)
    child._prompts_paused = True

    task = asyncio.create_task(
        child.request_user_selection("pd4", "field", "Push?", timeout=30)
    )
    await asyncio.sleep(0.05)
    assert parent.interactive_prompts_paused is False
    parent.resolve_user_selection("pd4", "yes")
    await task

    assert child.interactive_prompts_paused is False


async def test_grandchild_walks_up_to_the_same_sink():
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)
    grandchild = ConversationSession("c1", "p1", "u1", prompt_sink=child)

    task = asyncio.create_task(
        grandchild.request_user_selection("pd5", "field", "Push?", timeout=30)
    )
    await asyncio.sleep(0.05)

    assert [p["prompt_id"] for p in parent.pending_prompt_events()] == ["pd5"]
    assert parent.resolve_user_selection("pd5", "no") is True
    assert await task == "no"


async def test_secret_prompt_resolution_reaches_a_delegated_session():
    parent = ConversationSession("c1", "p1", "u1")
    child = ConversationSession("c1", "p1", "u1", prompt_sink=parent)

    task = asyncio.create_task(
        child.request_user_selection("pd6", "field", "API key?", secret=True, timeout=30)
    )
    await asyncio.sleep(0.05)

    assert parent.resolve_user_selection_secret("pd6", saved=True) is True
    # The plaintext never reaches the agent: an opaque confirmation is returned.
    assert await task == "secret_saved"
