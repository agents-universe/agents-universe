"""pending_prompt_payload: the WS handler's pre-resolve snapshot of a prompt.

The awaiting request_user_selection() pops the payload in its finally block,
so anything read after resolve_user_selection() is gone — the accessor must
return a copy while pending and None once answered or timed out.
"""
from __future__ import annotations

import asyncio

from agent_core.session import ConversationSession, UserSelectionTimeoutError


async def test_payload_visible_while_pending_and_none_after_resolve():
    session = ConversationSession("c", "p", "u")
    task = asyncio.create_task(
        session.request_user_selection(
            "prompt-1", "field", "Which environment?", timeout=30
        )
    )
    await asyncio.sleep(0.05)  # let the prompt register

    payload = session.pending_prompt_payload("prompt-1")
    assert payload is not None
    assert payload["question"] == "Which environment?"
    # A copy, not the live dict — mutating it must not corrupt the replay map.
    payload["question"] = "tampered"
    assert session.pending_prompt_payload("prompt-1")["question"] == (
        "Which environment?"
    )

    assert session.resolve_user_selection("prompt-1", "dev")
    assert await task == "dev"
    # The finally block pops the payload once the answer is consumed.
    await asyncio.sleep(0)
    assert session.pending_prompt_payload("prompt-1") is None
    assert session.pending_prompt_payload("unknown") is None


async def test_payload_none_after_timeout():
    session = ConversationSession("c", "p", "u")
    task = asyncio.create_task(
        session.request_user_selection(
            "prompt-2", "field", "Quick?", timeout=0.05
        )
    )
    await asyncio.sleep(0.1)
    try:
        await task
    except UserSelectionTimeoutError:
        pass
    assert session.pending_prompt_payload("prompt-2") is None
