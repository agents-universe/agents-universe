"""Abort attribution: every session abort records WHY (first cause wins).

Without this, an interrupted run cannot be told apart from a user Stop —
emit()'s backpressure self-abort in particular bypassed signal_abort entirely
and left no trace of what killed the agent.
"""
import asyncio
import logging

import agent_core.session as session_mod
from agent_core.session import ConversationSession, SessionEvent


async def test_abort_default_reason():
    s = ConversationSession("c1", "p1", "u1")
    s.abort()
    assert s.is_aborted()
    assert s.abort_reason == "abort"


async def test_abort_first_reason_wins():
    """The first cause is the interesting one; later signals must not
    overwrite it (a follow-up Stop after a backpressure abort would)."""
    s = ConversationSession("c1", "p1", "u1")
    s.abort("event_queue_blocked")
    s.abort("abort_signal")
    assert s.abort_reason == "event_queue_blocked"
    assert s.is_aborted()


async def test_emit_timeout_aborts_with_queue_reason(monkeypatch, caplog):
    """A stalled consumer must abort the session with an attributable reason
    and a log line that names the conversation and the backlog."""
    s = ConversationSession("c1", "p1", "u1")
    # Fill the queue so put() blocks for the whole (shrunk) timeout window.
    for _ in range(s._event_queue.maxsize):
        s._event_queue.put_nowait(SessionEvent(type="stream_delta", data={"delta": "x"}))
    monkeypatch.setattr(session_mod, "_EMIT_PUT_TIMEOUT_S", 0.05)

    with caplog.at_level(logging.WARNING, logger="agent_core.session"):
        await s.emit("stream_delta", delta="y")

    assert s.is_aborted()
    assert s.abort_reason == "event_queue_blocked"
    assert "Event queue blocked" in caplog.text
    assert "conversation=c1" in caplog.text
