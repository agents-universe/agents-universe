"""Session.close() queue-drain semantics: persistence-critical events survive
a full queue (consumer stalled), UI-only events are dropped."""
import asyncio

from agent_core.session import ConversationSession, SessionEvent


def _fill(session: ConversationSession, events: list[tuple[str, dict]]):
    for etype, data in events:
        session._event_queue.put_nowait(SessionEvent(type=etype, data=data))


async def _drain(session: ConversationSession) -> list[str]:
    types = []
    while True:
        ev = await session._event_queue.get()
        if ev is None:
            return types
        types.append(ev.type)


async def test_close_keeps_user_message_injected_when_queue_full():
    """user_message_injected is the ONLY writer of an injected message's DB
    row — dropping it (UI-only classification) would erase the user's words
    from history and the LLM context when the consumer stalls."""
    s = ConversationSession("c1", "p1", "u1")
    _fill(s, [("progress", {"i": i}) for i in range(999)])
    _fill(s, [("user_message_injected", {"message_id": "m1"})])
    assert s._event_queue.full()

    await s.close()

    types = await _drain(s)
    assert "user_message_injected" in types
    assert "progress" not in types


async def test_close_keeps_terminal_task_and_stream_events_when_queue_full():
    """stream_delta/stream_end carry the reply text; task_completed is the
    only source of a task row's final status — all must survive."""
    s = ConversationSession("c1", "p1", "u1")
    _fill(s, [("tool_call_start", {"call_id": f"c{i}"}) for i in range(997)])
    _fill(s, [
        ("stream_delta", {"delta": "final text"}),
        ("task_completed", {"task_id": "t1"}),
        ("stream_end", {"message_id": "m9"}),
    ])
    assert s._event_queue.full()

    await s.close()

    types = await _drain(s)
    assert "stream_delta" in types
    assert "task_completed" in types
    assert "stream_end" in types
    assert types[-1] == "stream_end"  # sentinel path: end marker last
    assert "tool_call_start" not in types
