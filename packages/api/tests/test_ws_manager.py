"""Unit tests for ConnectionManager multi-conversation support.

Verifies the core behaviours that enable background agent execution:
- disconnect() does NOT remove session/abort/memories while agent is running
- deregister_session() cleans up only when no WS is connected
- send() routes to the current registered WS (supports reconnection)
- is_session_active() / get_running_conversations() queries
- connect() preserves existing abort events
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.websocket.manager import ConnectionManager, _json_default
from datetime import datetime


# ── _json_default ──────────────────────────────────────────────────

def test_json_default_handles_datetime():
    dt = datetime(2026, 1, 15, 10, 30, 0)
    assert _json_default(dt) == "2026-01-15T10:30:00"


def test_json_default_raises_on_unknown():
    with pytest.raises(TypeError):
        _json_default(object())


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def mgr():
    return ConnectionManager()


def _make_ws():
    """Create a mock WebSocket with AsyncMock for send methods."""
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _make_session(user_id: str = "u1"):
    """Create a mock ConversationSession."""
    sess = MagicMock()
    sess.user_id = user_id
    sess.current_streaming_text = ""
    sess.current_tool_calls = []
    return sess


# ── connect / disconnect ───────────────────────────────────────────

async def test_connect_registers_ws_and_creates_abort_event(mgr):
    ws = _make_ws()
    await mgr.connect("c1", ws)
    assert mgr._connections["c1"] is ws
    assert "c1" in mgr._abort_events


async def test_connect_preserves_existing_abort_event(mgr):
    """A reconnect must NOT replace the abort event - the background
    agent's _watch_abort is waiting on the original one."""
    ws1 = _make_ws()
    await mgr.connect("c1", ws1)
    original_event = mgr._abort_events["c1"]
    original_event.set()

    ws2 = _make_ws()
    await mgr.connect("c1", ws2)
    # Same event object, still set
    assert mgr._abort_events["c1"] is original_event
    assert original_event.is_set()


async def test_disconnect_removes_ws_but_keeps_session(mgr):
    """When agent is running, disconnect keeps session/abort/memories."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    mgr.register_session("c1", _make_session())
    mgr.add_session_memory("c1", "note", 123.0)

    await mgr.disconnect("c1", ws)

    assert "c1" not in mgr._connections
    assert "c1" in mgr._sessions  # session preserved
    assert "c1" in mgr._abort_events  # abort event preserved
    assert "c1" in mgr._session_memories  # memories preserved


async def test_disconnect_cleans_all_when_no_session(mgr):
    """When no agent is running, disconnect cleans everything."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    mgr.add_session_memory("c1", "note", 123.0)

    await mgr.disconnect("c1", ws)

    assert "c1" not in mgr._connections
    assert "c1" not in mgr._abort_events
    assert "c1" not in mgr._session_memories


async def test_disconnect_ignores_stale_ws(mgr):
    """If a newer WS has replaced the old one, disconnect(old_ws) is a no-op."""
    ws1 = _make_ws()
    await mgr.connect("c1", ws1)
    ws2 = _make_ws()
    await mgr.connect("c1", ws2)  # replaces ws1

    await mgr.disconnect("c1", ws1)  # should be ignored

    assert mgr._connections["c1"] is ws2  # ws2 still registered


# ── deregister_session ─────────────────────────────────────────────

def test_deregister_session_removes_session_only_when_ws_connected(mgr):
    """If WS is still connected, only session is removed - abort/memories stay."""
    ws = _make_ws()
    mgr._connections["c1"] = ws
    mgr._sessions["c1"] = _make_session()
    mgr._abort_events["c1"] = asyncio.Event()
    mgr._session_memories["c1"] = [{"note": "x", "timestamp": 1}]

    mgr.deregister_session("c1")

    assert "c1" not in mgr._sessions
    assert "c1" in mgr._abort_events  # preserved for future messages
    assert "c1" in mgr._session_memories


def test_deregister_session_cleans_all_when_no_ws(mgr):
    """If no WS is connected, everything is cleaned up."""
    mgr._sessions["c1"] = _make_session()
    mgr._abort_events["c1"] = asyncio.Event()
    mgr._session_memories["c1"] = [{"note": "x", "timestamp": 1}]

    mgr.deregister_session("c1")

    assert "c1" not in mgr._sessions
    assert "c1" not in mgr._abort_events
    assert "c1" not in mgr._session_memories


# ── send ───────────────────────────────────────────────────────────

async def test_send_delivers_to_registered_ws(mgr):
    ws = _make_ws()
    await mgr.connect("c1", ws)

    result = await mgr.send("c1", {"type": "stream_delta", "delta": "hi"})

    assert result is True
    ws.send_text.assert_called_once()
    sent_data = ws.send_text.call_args[0][0]
    assert '"stream_delta"' in sent_data
    assert '"hi"' in sent_data


async def test_send_returns_false_when_no_ws(mgr):
    result = await mgr.send("c1", {"type": "ping"})
    assert result is False


async def test_send_evicts_dead_ws(mgr):
    """If send fails, the dead WS is removed so a reconnect can register a new one."""
    ws = _make_ws()
    ws.send_text.side_effect = RuntimeError("connection closed")
    await mgr.connect("c1", ws)

    result = await mgr.send("c1", {"type": "ping"})

    assert result is False
    assert "c1" not in mgr._connections  # dead WS evicted


async def test_send_timeout_evicts_half_open_ws(mgr, monkeypatch, caplog):
    """A half-open TCP connection (client vanished, no RST) blocks send
    forever — the timeout must evict it like a dead socket instead of
    stalling forward_events, which would fill the session queue and make
    emit() abort the run as "consumer likely dead"."""
    import api.websocket.manager as mgr_mod
    import logging

    monkeypatch.setattr(mgr_mod, "_SEND_TIMEOUT_S", 0.05)
    ws = _make_ws()

    async def _hang(_payload):
        await asyncio.sleep(60)

    ws.send_text.side_effect = _hang
    await mgr.connect("c1", ws)

    with caplog.at_level(logging.WARNING, logger="agents_universe.ws"):
        result = await mgr.send("c1", {"type": "ping"})

    assert result is False
    assert "c1" not in mgr._connections  # half-open WS evicted
    ws.close.assert_awaited()
    assert "WS send timed out" in caplog.text
    assert "c1" in caplog.text


async def test_send_routes_to_reconnected_ws(mgr):
    """After reconnect, send() delivers to the new WS, not the old one."""
    ws1 = _make_ws()
    await mgr.connect("c1", ws1)
    ws2 = _make_ws()
    await mgr.connect("c1", ws2)  # reconnect

    await mgr.send("c1", {"type": "stream_delta", "delta": "x"})

    ws1.send_text.assert_not_called()  # old WS not used
    ws2.send_text.assert_called_once()  # new WS used


# ── is_session_active / get_running_conversations ─────────────────

def test_is_session_active_false_by_default(mgr):
    assert not mgr.is_session_active("c1")


def test_is_session_active_true_after_register(mgr):
    mgr.register_session("c1", _make_session())
    assert mgr.is_session_active("c1")


def test_is_session_active_false_after_deregister(mgr):
    mgr.register_session("c1", _make_session())
    mgr.deregister_session("c1")
    assert not mgr.is_session_active("c1")


def test_get_running_conversations_filters_by_user(mgr):
    mgr.register_session("c1", _make_session(user_id="u1"))
    mgr.register_session("c2", _make_session(user_id="u2"))
    mgr.register_session("c3", _make_session(user_id="u1"))

    result = mgr.get_running_conversations("u1")
    assert result == {"c1", "c3"}


def test_get_running_conversations_empty(mgr):
    assert mgr.get_running_conversations("u1") == set()


# ── Full lifecycle: disconnect → reconnect → agent finishes ───────

async def test_lifecycle_disconnect_reconnect_finish(mgr):
    """Simulates: WS disconnects, user reconnects, agent finishes, WS disconnects."""
    ws1 = _make_ws()
    await mgr.connect("c1", ws1)
    sess = _make_session()
    mgr.register_session("c1", sess)
    mgr.add_session_memory("c1", "note", 1.0)

    # 1. WS disconnects - agent still running
    await mgr.disconnect("c1", ws1)
    assert mgr.is_session_active("c1")  # agent still running
    assert not mgr.get_session("c1") is None

    # 2. User reconnects
    ws2 = _make_ws()
    await mgr.connect("c1", ws2)
    assert mgr._connections["c1"] is ws2

    # 3. Agent finishes
    mgr.deregister_session("c1")
    assert not mgr.is_session_active("c1")
    # WS still connected, so abort/memories preserved
    assert "c1" in mgr._abort_events

    # 4. WS disconnects - everything cleaned up
    await mgr.disconnect("c1", ws2)
    assert "c1" not in mgr._abort_events
    assert "c1" not in mgr._session_memories


# ── Abort event survives disconnect ────────────────────────────────

async def test_abort_works_after_reconnect(mgr):
    """User can abort a background agent after reconnecting."""
    ws1 = _make_ws()
    await mgr.connect("c1", ws1)
    mgr.register_session("c1", _make_session())

    # Disconnect
    await mgr.disconnect("c1", ws1)

    # Reconnect
    ws2 = _make_ws()
    await mgr.connect("c1", ws2)

    # Abort should work (same event object)
    abort_event = mgr.get_abort_event("c1")
    assert abort_event is not None
    assert not abort_event.is_set()

    mgr.signal_abort("c1")
    assert abort_event.is_set()


# ── Abort reason attribution ───────────────────────────────────────

def test_signal_abort_records_first_reason(mgr):
    """signal_abort records a stable reason token; the first cause wins so a
    follow-up signal cannot overwrite the original attribution."""
    mgr.ensure_abort_event("c1")

    mgr.signal_abort("c1", reason="ws_abort_frame")
    mgr.signal_abort("c1", reason="publish_abort_api")

    assert mgr.get_abort_event("c1").is_set()
    assert mgr.get_abort_reason("c1") == "ws_abort_frame"


def test_reset_abort_clears_reason(mgr):
    mgr.ensure_abort_event("c1")
    mgr.signal_abort("c1", reason="ws_abort_frame")
    mgr.reset_abort("c1")
    assert not mgr.get_abort_event("c1").is_set()
    assert mgr.get_abort_reason("c1") is None


async def test_disconnect_cleans_abort_reason(mgr):
    """The reason is popped wherever the event is popped — a stale token must
    not leak into the next turn on the same conversation."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    mgr.signal_abort("c1", reason="ws_abort_frame")
    assert mgr.get_abort_reason("c1") == "ws_abort_frame"

    await mgr.disconnect("c1", ws)  # no session, no claim → full cleanup

    assert "c1" not in mgr._abort_events
    assert mgr.get_abort_reason("c1") is None


def test_deregister_session_cleans_abort_reason(mgr):
    mgr._abort_events["c1"] = asyncio.Event()
    mgr.signal_abort("c1", reason="ws_abort_frame")

    mgr.deregister_session("c1")  # no WS connected → full cleanup

    assert "c1" not in mgr._abort_events
    assert mgr.get_abort_reason("c1") is None


async def test_disconnect_during_claim_window_keeps_abort_event(mgr):
    """A socket drop between claim_turn() and register_session() must not
    drop the abort event: the turn is already in flight and run_turn wires
    its abort watcher to that event, so popping it made Stop a no-op."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    assert await mgr.claim_turn("c1") is True
    assert mgr.get_session("c1") is None  # still in the history-load window

    await mgr.disconnect("c1", ws)

    assert "c1" not in mgr._connections
    event = mgr.get_abort_event("c1")
    assert event is not None  # watcher can still be wired to it
    mgr.signal_abort("c1")
    assert event.is_set()

    # The turn ends with no socket connected -> cleanup resumes as before.
    mgr.release_turn("c1")
    mgr.deregister_session("c1")
    assert "c1" not in mgr._abort_events


async def test_disconnect_after_release_turn_cleans_up(mgr):
    """The claim guard must not keep abort events alive after the turn ends."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    await mgr.claim_turn("c1")
    mgr.release_turn("c1")

    await mgr.disconnect("c1", ws)

    assert "c1" not in mgr._abort_events


# ── Session memory survives disconnect ─────────────────────────────

async def test_session_memory_survives_disconnect(mgr):
    """Session memories persist through disconnect when agent is running."""
    ws = _make_ws()
    await mgr.connect("c1", ws)
    mgr.register_session("c1", _make_session())
    mgr.add_session_memory("c1", "important note", 123.0)

    await mgr.disconnect("c1", ws)

    memories = mgr.get_session_memories("c1")
    assert len(memories) == 1
    assert memories[0]["note"] == "important note"
