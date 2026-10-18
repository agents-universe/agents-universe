"""_NullTransport must keep the turn's REAL stream_end, not a synthetic tail.

run_turn emits more than one stream_end frame on some paths: the abort tail
carries message_id=None, and the outer BaseException handler emits a fresh
err_msg_id that was never persisted. _last_reply trusts only
transport.stream_end["message_id"], so a synthetic tail clobbering the real
frame scored the run "The agent produced no reply" and deliver_result posted
that as a SECOND assistant message right after the reply already in history.
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
