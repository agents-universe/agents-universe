"""Parallel plan tasks must not share one Playwright page.

``ToolContext.copy_for_task`` hands every parallel task a clone whose owner is
the session context, and browser_playwright stored the page on that owner — so
task A's goto replaced task B's page and every later get_text/screenshot in
either task read the wrong document.
"""
from __future__ import annotations

import pytest

from agent_core.tools.browser_playwright import BrowserPlaywrightTool
from browser_fakes import install_browser, make_context


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    install_browser(monkeypatch, context)
    return context


async def test_parallel_task_clones_do_not_share_a_page(ctx):
    tool = BrowserPlaywrightTool()
    task_a = ctx.copy_for_task("t1", 1)
    task_b = ctx.copy_for_task("t2", 2)

    await tool.execute({"operation": "goto", "url": "http://site-a.test/"}, task_a)
    await tool.execute({"operation": "goto", "url": "http://site-b.test/"}, task_b)

    assert len(ctx._fake_browser.pages) == 2  # one page per task
    assert len(ctx._fake_browser.contexts) == 2  # …each in its own context
    read_a = await tool.execute({"operation": "get_text"}, task_a)
    read_b = await tool.execute({"operation": "get_text"}, task_b)
    assert read_a["text"] == "CONTENT-OF http://site-a.test/"
    assert read_b["text"] == "CONTENT-OF http://site-b.test/"


async def test_cleanup_closes_every_task_page_and_context(ctx):
    tool = BrowserPlaywrightTool()
    task_a = ctx.copy_for_task("t1", 1)
    task_b = ctx.copy_for_task("t2", 2)
    await tool.execute({"operation": "goto", "url": "http://site-a.test/"}, task_a)
    await tool.execute({"operation": "goto", "url": "http://site-b.test/"}, task_b)

    await ctx.cleanup()

    assert ctx._fake_browser.pages
    assert all(p.closed for p in ctx._fake_browser.pages)
    # Contexts carry the recording finalization: a context left open at
    # cleanup means a video that was never written.
    assert ctx._fake_browser.contexts
    assert all(c.closed for c in ctx._fake_browser.contexts)


async def test_ssrf_route_is_registered_once_per_page(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "goto", "url": "http://site.test/"}, ctx)
    page = ctx._browser_page
    assert page.routes == ["**/*"]

    # Later operations reuse the same page and must not stack another route.
    await tool.execute({"operation": "get_text"}, ctx)
    assert page.routes == ["**/*"]


async def test_page_rebuilt_on_a_surviving_context_is_still_guarded(ctx):
    """A page can die while its context lives — the SSRF redirect block does
    exactly that — and the replacement page must carry the route too, or every
    later operation browses unguarded."""
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "goto", "url": "http://site.test/"}, ctx)
    live_context = ctx._browser_context
    await ctx._browser_page.close()
    ctx._browser_page = None

    await tool.execute({"operation": "get_text"}, ctx)

    assert ctx._browser_context is live_context  # reused, not rebuilt
    assert ctx._browser_page is not None
    assert ctx._browser_page.routes == ["**/*"]
