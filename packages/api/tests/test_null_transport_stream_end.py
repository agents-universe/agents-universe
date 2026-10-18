"""_NullTransport must keep the turn's REAL stream_end, not a synthetic tail.

run_turn emits more than one stream_end frame on some paths: a mid-run
injection freezes the partial under a STEP-boundary id (injection=True),
the abort tail carries message_id=None, and the outer BaseException handler
emits a fresh err_msg_id that was never persisted. _last_reply trusts only
transport.stream_end["message_id"], so a synthetic tail clobbering the real
frame scored the run "The agent produced no reply" and deliver_result posted
that as a SECOND assistant message right after the reply already in history;
an injection boundary latching instead of the terminal frame scored a
successful run the same wrong way.
"""
from __future__ import annotations

import pytest

from api.services.scheduled_runs import _NullTransport


@pytest.mark.asyncio
async def test_real_stream_end_survives_later_idless_abort_tail():
    t = _NullTransport()
    await t.send("c", {"type": "stream_end", "message_id": "m-real"})
    await t.send("c", {"type": "stream_end", "message_id": None})  # abort tail
    assert t.stream_end["message_id"] == "m-real"


@pytest.mark.asyncio
async def test_real_stream_end_survives_later_synthetic_error_tail():
    # The outer-exception tail's err_msg_id is truthy but never written to
    # messages — the first id-bearing frame must still win.
    t = _NullTransport()
    await t.send("c", {"type": "stream_end", "message_id": "m-real"})
    await t.send("c", {"type": "stream_end", "message_id": "err-synthetic"})
    assert t.stream_end["message_id"] == "m-real"


@pytest.mark.asyncio
async def test_idless_frame_is_upgraded_by_a_later_real_id():
    # Ordering between the drain-forwarded stream_end and the tails is not
    # fixed — an id-less frame recorded first must not block the real one.
    t = _NullTransport()
    await t.send("c", {"type": "stream_end", "message_id": None})
    await t.send("c", {"type": "stream_end", "message_id": "m-real"})
    assert t.stream_end["message_id"] == "m-real"


@pytest.mark.asyncio
async def test_idless_only_still_records_the_frame():
    # A genuinely aborted turn with no persisted reply: _last_reply reads
    # message_id=None → ("", False), same behavior as before the fix.
    t = _NullTransport()
    await t.send("c", {"type": "stream_end", "message_id": None})
    assert t.stream_end is not None
    assert t.stream_end["message_id"] is None


@pytest.mark.asyncio
async def test_error_frames_still_recorded():
    t = _NullTransport()
    await t.send("c", {"type": "error", "message": "boom"})
    assert t.error == "boom"


@pytest.mark.asyncio
async def test_injection_boundary_yields_to_the_real_terminal_frame():
    # A mid-run injection (claim-window drain) freezes the partial under a
    # step-boundary id BEFORE the turn's real terminal frame — the scorer
    # must latch on the terminal frame, not the frozen partial.
    t = _NullTransport()
    await t.send("c", {
        "type": "stream_end", "message_id": "m-partial",
        "stop_reason": "interrupted", "injection": True,
    })
    await t.send("c", {"type": "stream_end", "message_id": "m-final"})
    assert t.stream_end["message_id"] == "m-final"


@pytest.mark.asyncio
async def test_injection_boundary_records_only_as_fallback():
    # Turn died right after the boundary: the boundary id is the only
    # signal left (best effort — its row may not exist).
    t = _NullTransport()
    await t.send("c", {
        "type": "stream_end", "message_id": "m-partial", "injection": True,
    })
    assert t.stream_end["message_id"] == "m-partial"
    # ...and a later real frame still upgrades the fallback.
    await t.send("c", {"type": "stream_end", "message_id": "m-final"})
    assert t.stream_end["message_id"] == "m-final"


@pytest.mark.asyncio
async def test_real_frame_is_never_downgraded_by_an_injection_boundary():
    t = _NullTransport()
    await t.send("c", {"type": "stream_end", "message_id": "m-final"})
    await t.send("c", {
        "type": "stream_end", "message_id": "m-partial", "injection": True,
    })
    assert t.stream_end["message_id"] == "m-final"
