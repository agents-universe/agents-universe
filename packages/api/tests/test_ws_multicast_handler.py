"""Multi-tab WebSocket: several sockets share one conversation.

The old manager kept a SINGLE socket per conversation and closed the
previous one with 4409 whenever a new tab connected — two tabs on the same
conversation kicked each other in a ~1s reconnect loop (the frontend resets
its backoff on every successful open). These tests drive the real
``conversation_ws`` endpoint with two fake sockets and pin the multicast
semantics: both tabs stay open, both receive every frame, and one tab
leaving never interrupts the other. Also covers the 4001 auth denial, which
must ACCEPT before closing so the browser actually sees the code.
"""
from __future__ import annotations

import asyncio
import json

from api.websocket.handlers import _deny_unauthenticated, conversation_ws
from api.websocket.manager import manager


class _FakeSocket:
    """Just enough of a WebSocket for conversation_ws to run against."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.frames: asyncio.Queue = asyncio.Queue()
        self.cookies: dict = {}
        self.accepted = False
        self.closed_with: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        return await self.frames.get()

    async def close(self, code: int = 1000) -> None:
        self.closed_with.append(code)

    def types(self) -> list[str]:
        return [f.get("type") for f in self.sent]


async def _wait_registered(conv_id: str, ws: _FakeSocket, timeout_s: float = 1.0) -> None:
    for _ in range(200):
        await asyncio.sleep(0.005)
        if ws in manager._connections.get(conv_id, ()):
            return
    raise AssertionError(f"socket never registered for {conv_id}")


async def _open_sockets(conv_id: str, *sockets: _FakeSocket) -> list[asyncio.Task]:
    tasks = [asyncio.create_task(conversation_ws(conv_id, ws)) for ws in sockets]
    for ws in sockets:
        await _wait_registered(conv_id, ws)
    return tasks


async def _cleanup(conv_id: str, sockets: list[_FakeSocket], tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for ws in sockets:
        await manager.disconnect(conv_id, ws)
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_two_tabs_both_connect_without_being_kicked(db, make_project):
    project = await make_project("mcast")
    from api.models.conversation import Conversation

    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    conv_id = conv.conversation_id

    ws1, ws2 = _FakeSocket(), _FakeSocket()
    tasks = await _open_sockets(conv_id, ws1, ws2)
    try:
        # Neither tab was kicked — no 4409 close frame anywhere.
        assert ws1.accepted and ws2.accepted
        assert 4409 not in ws1.closed_with
        assert 4409 not in ws2.closed_with
        assert manager._connections[conv_id] == {ws1, ws2}

        # A frame reaches BOTH tabs.
        delivered = await manager.send(conv_id, {"type": "stream_delta", "delta": "hi"})
        assert delivered is True
        assert "stream_delta" in ws1.types()
        assert "stream_delta" in ws2.types()
    finally:
        await _cleanup(conv_id, [ws1, ws2], tasks)


async def test_one_tab_leaving_never_interrupts_the_other(db, make_project):
    project = await make_project("mcast2")
    from api.models.conversation import Conversation

    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    conv_id = conv.conversation_id

    ws1, ws2 = _FakeSocket(), _FakeSocket()
    tasks = await _open_sockets(conv_id, ws1, ws2)
    try:
        # Tab 1 closes; tab 2 keeps streaming.
        await manager.disconnect(conv_id, ws1)
        assert manager._connections[conv_id] == {ws2}

        delivered = await manager.send(conv_id, {"type": "stream_delta", "delta": "still here"})
        assert delivered is True
        assert "stream_delta" in ws2.types()
        assert conv_id not in manager._connections or ws1 not in manager._connections[conv_id]
    finally:
        await _cleanup(conv_id, [ws1, ws2], tasks)


async def test_deny_unauthenticated_accepts_then_closes_with_4001():
    """Pre-accept close(4001) is answered by uvicorn as HTTP 403 — the
    browser only sees 1006 and keeps retrying forever. The denial must
    accept first, emit an error frame, then close with 4001."""
    ws = _FakeSocket()

    await _deny_unauthenticated(ws)

    assert ws.accepted is True
    assert ws.sent and ws.sent[0]["code"] == "WS_UNAUTHENTICATED"
    assert ws.closed_with == [4001]
