"""browser_playwright evaluate must respect a timeout (fake page — no Chromium).

``page.evaluate`` has no timeout of its own: a script that never resolves
(``await new Promise(() => {})``) parked the tool call forever, and the agent
loop awaits ``tool.execute`` with no global timeout, so the whole turn hung
with it.
"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.browser_playwright import BrowserPlaywrightTool


class HangingPage:
    """Page whose evaluate() never resolves — like ``new Promise(() => {})``."""

    def __init__(self):
        self.closed = False

    @property
    def url(self) -> str:
        return "http://site.test/"

    async def title(self) -> str:
        return "t"  # _get_page probes title() to decide the page is alive

    async def evaluate(self, script: str, arg=None):
        await asyncio.Event().wait()  # never set

    async def close(self) -> None:
        self.closed = True


def make_context(page) -> ToolContext:
    ctx = ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
    )
    ctx._browser_page = page
    return ctx


@pytest.mark.asyncio
async def test_evaluate_never_hangs_past_the_clamped_timeout(monkeypatch):
    page = HangingPage()
    ctx = make_context(page)

    async def _ensure_browser(self=None):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    # Outer 5s safety net so a PRE-fix run fails instead of hanging: the
    # clamped minimum timeout is 1000ms, so a fixed tool answers well before.
    result = await asyncio.wait_for(
        BrowserPlaywrightTool().execute(
            {
                "operation": "evaluate",
                "script": "new Promise(() => {})",
                "timeout": 1000,
            },
            ctx,
        ),
        timeout=5,
    )
    assert "error" in result, result
    assert "timed out" in result["error"].lower()
    assert page.closed is False  # a timeout is not a crash — page stays usable
