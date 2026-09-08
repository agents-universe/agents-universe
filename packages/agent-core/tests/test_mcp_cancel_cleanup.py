"""Cancellation during MCP attach must not leak live connections.

Two leak paths:
- ``McpServerSession.start()`` spawns a background task; when start() is
  cancelled the task is still holding an open transport and nobody awaits it.
- ``discover_tools()`` registered sessions only after ``gather()`` returned —
  a cancelled gather discards the sessions that had already connected, and
  ``close_all()`` only ever sees registered ones.
"""
from __future__ import annotations

import asyncio

import pytest

import agent_core.tools.mcp_client as mcp_client
from agent_core.tools.base import ToolContext
from agent_core.tools.mcp_client import McpConnectionManager, McpServerSession


def _ctx() -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path="/tmp/test",
        conversation_id="c1",
        user_id="u1",
    )


class _BlockingSession(McpServerSession):
    """Real session class with ``_run`` replaced by a never-ending await."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def _run(self) -> None:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.exited.set()


async def test_start_cancellation_stops_background_task():
    sess = _BlockingSession(
        "s", {"url": "http://x", "connect_timeout_seconds": 30}, {}
    )
    task = asyncio.create_task(sess.start())
    await asyncio.wait_for(sess.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(sess.exited.wait(), timeout=1)  # transport unwound
    assert sess._task is None


class _FakeSession:
    """Stands in for McpServerSession; ``block: True`` hangs in start()."""

    instances: list["_FakeSession"] = []

    def __init__(self, slug, cfg, headers, ssl_verify=True):
        self.slug = slug
        self.cfg = cfg
        self.closed = False
        self.tools = []
        self.started = asyncio.Event()
        _FakeSession.instances.append(self)

    async def start(self) -> None:
        self.started.set()
        if self.cfg.get("block"):
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


def _patch_discovery(monkeypatch) -> None:
    _FakeSession.instances = []
    monkeypatch.setattr(mcp_client, "McpServerSession", _FakeSession)

    async def _headers(_ctx, _cfg):
        return {}

    monkeypatch.setattr(mcp_client, "_resolve_auth_headers", _headers)
    monkeypatch.setattr(mcp_client, "_validate_mcp_url", lambda *a, **k: None)


async def _wait_for_sessions(count: int = 2) -> None:
    for _ in range(200):
        if len(_FakeSession.instances) == count and all(
            s.started.is_set() for s in _FakeSession.instances
        ):
            return
        await asyncio.sleep(0)
    pytest.fail("fake sessions did not start")


def _session(slug: str) -> _FakeSession:
    return next(s for s in _FakeSession.instances if s.slug == slug)


_SERVERS = {
    "fast": {"url": "http://fast"},
    "slow": {"url": "http://slow", "block": True},
}


async def test_discover_tools_registers_connected_sessions_before_gather(monkeypatch):
    """A connected session must survive a cancelled gather so close_all()
    can still shut it down."""
    _patch_discovery(monkeypatch)
    mgr = McpConnectionManager(_ctx())
    task = asyncio.create_task(mgr.discover_tools(dict(_SERVERS)))
    await _wait_for_sessions()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await mgr.close_all()
    assert _session("fast").closed


async def test_attach_mcp_tools_cancellation_closes_connected_sessions(monkeypatch):
    """The turn is stopped while MCP is attaching: agent.run() (which owns
    cleanup()) never started, so attach itself must close what connected."""
    _patch_discovery(monkeypatch)

    async def _load(_ctx):
        return dict(_SERVERS)

    monkeypatch.setattr(mcp_client, "load_mcp_servers", _load)

    ctx = _ctx()
    task = asyncio.create_task(mcp_client.attach_mcp_tools(ctx, ["mcp"]))
    await _wait_for_sessions()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _session("fast").closed
