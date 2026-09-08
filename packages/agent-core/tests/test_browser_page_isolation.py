"""Parallel plan tasks must not share one Playwright page.

``ToolContext.copy_for_task`` hands every parallel task a clone whose owner is
the session context, and browser_playwright stored the page on that owner — so
task A's goto replaced task B's page and every later get_text/screenshot in
either task read the wrong document.
"""
from __future__ import annotations

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.browser_playwright import BrowserPlaywrightTool


class FakePage:
    def __init__(self):
        self._url = "about:blank"
        self.closed = False

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url: str, timeout: int = 30000):
        self._url = url
        return None

    async def wait_for_load_state(self, state: str) -> None:
        return None

    async def title(self) -> str:
        return self._url

    async def route(self, pattern: str, handler) -> None:
        return None

    def on(self, event: str, handler) -> None:
        return None

    def remove_listener(self, event: str, handler) -> None:
        return None

    async def inner_text(self, selector: str) -> str:
        return f"CONTENT-OF {self._url}"

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.pages: list[FakePage] = []

    async def new_page(self, **kwargs) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


@pytest.fixture
def ctx(monkeypatch):
    context = ToolContext(
        project_id="p", project_fs_path="/tmp/p", conversation_id="c", user_id="u"
    )
    browser = FakeBrowser()

    async def _ensure_browser(self=None):
        return browser

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)
    context._fake_browser = browser
    return context


async def test_parallel_task_clones_do_not_share_a_page(ctx):
    tool = BrowserPlaywrightTool()
    task_a = ctx.copy_for_task("t1", 1)
    task_b = ctx.copy_for_task("t2", 2)

    await tool.execute({"operation": "goto", "url": "http://site-a.test/"}, task_a)
    await tool.execute({"operation": "goto", "url": "http://site-b.test/"}, task_b)

    assert len(ctx._fake_browser.pages) == 2  # one page per task
    read_a = await tool.execute({"operation": "get_text"}, task_a)
    read_b = await tool.execute({"operation": "get_text"}, task_b)
    assert read_a["text"] == "CONTENT-OF http://site-a.test/"
    assert read_b["text"] == "CONTENT-OF http://site-b.test/"


async def test_cleanup_closes_every_task_page(ctx):
    tool = BrowserPlaywrightTool()
    task_a = ctx.copy_for_task("t1", 1)
    task_b = ctx.copy_for_task("t2", 2)
    await tool.execute({"operation": "goto", "url": "http://site-a.test/"}, task_a)
    await tool.execute({"operation": "goto", "url": "http://site-b.test/"}, task_b)

    await ctx.cleanup()

    assert ctx._fake_browser.pages
    assert all(p.closed for p in ctx._fake_browser.pages)
