"""browser_playwright goto error reporting (fake page — no Chromium needed).

A failed navigation used to be reported as an SSRF block: the post-hoc check
ran against ``page.url``, which Playwright leaves at ``about:blank`` when
goto() throws, and the scheme check on ``about:`` raised SSRFError. Agents
then chased a non-existent SSRF problem instead of the DNS/connection error.
"""
from __future__ import annotations

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.browser_playwright import BrowserPlaywrightTool


class FakePage:
    """Minimal Playwright Page stand-in for the goto path."""

    def __init__(self, url: str = "about:blank", goto_error: Exception | None = None):
        self._url = url
        self._goto_error = goto_error
        self.closed = False
        self.goto_calls: list[dict] = []

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url: str, timeout: int = 30000, wait_until: str | None = None):
        self.goto_calls.append({"url": url, "timeout": timeout, "wait_until": wait_until})
        if self._goto_error is not None:
            raise self._goto_error
        self._url = url
        return None

    async def wait_for_load_state(self, state: str) -> None:
        return None

    async def title(self) -> str:
        return "t"

    def on(self, event: str, handler) -> None:
        return None

    def remove_listener(self, event: str, handler) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_context(page: FakePage) -> ToolContext:
    ctx = ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
    )
    ctx._browser_page = page
    return ctx


@pytest.mark.asyncio
async def test_failed_goto_reports_navigation_error_not_ssrf(monkeypatch):
    page = FakePage(goto_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://does-not-exist.invalid/"}, ctx
    )
    assert "error" in result, result
    assert "Navigation failed" in result["error"]
    assert "ERR_NAME_NOT_RESOLVED" in result["error"]
    assert "SSRF" not in result["error"]


@pytest.mark.asyncio
async def test_failed_goto_on_a_page_with_history_still_reports_error(monkeypatch):
    """A failed goto keeps page.url at the PREVIOUS page, not about:blank —
    the post-hoc SSRF check then passes on the old http(s) URL and the
    fall-through returned a success-shaped result (previous title/url,
    status None, no error key), so the agent kept reading the stale page."""
    page = FakePage(
        url="http://site.test/",
        goto_error=RuntimeError("net::ERR_NAME_NOT_RESOLVED"),
    )
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://does-not-exist.invalid/"}, ctx
    )
    assert "error" in result, result
    assert "Navigation failed" in result["error"]
    assert "ERR_NAME_NOT_RESOLVED" in result["error"]
    assert "SSRF" not in result["error"]


@pytest.mark.asyncio
async def test_goto_defaults_to_domcontentloaded(monkeypatch):
    """Playwright's 'load' default waits on every subresource; one stalled
    beacon on a slow network spends the whole timeout while the DOM is ready."""
    page = FakePage(url="http://site.test/")
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://site.test/"}, ctx
    )
    assert "error" not in result, result
    assert page.goto_calls[-1]["wait_until"] == "domcontentloaded"


@pytest.mark.asyncio
async def test_goto_honours_explicit_wait_until(monkeypatch):
    """Pages that genuinely need every subresource can still ask for 'load'."""
    page = FakePage(url="http://site.test/")
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://site.test/", "wait_until": "load"}, ctx
    )
    assert "error" not in result, result
    assert page.goto_calls[-1]["wait_until"] == "load"


@pytest.mark.asyncio
async def test_goto_unknown_wait_until_falls_back_to_default(monkeypatch):
    """An unknown value must not reach Playwright, which raises on it and would
    surface as a navigation failure the agent cannot act on."""
    page = FakePage(url="http://site.test/")
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://site.test/", "wait_until": "bogus"}, ctx
    )
    assert "error" not in result, result
    assert page.goto_calls[-1]["wait_until"] == "domcontentloaded"


@pytest.mark.asyncio
async def test_redirect_onto_metadata_ip_still_reported_as_ssrf(monkeypatch):
    """goto() aborts on the blocked redirect, but the page did land on the
    internal URL — that IS an SSRF block and must keep saying so."""
    page = FakePage(
        url="http://169.254.169.254/latest/meta-data/",
        goto_error=RuntimeError("net::ERR_ABORTED"),
    )
    ctx = make_context(page)

    async def _ensure_browser(self):
        return object()

    monkeypatch.setattr(ToolContext, "ensure_browser", _ensure_browser)

    result = await BrowserPlaywrightTool().execute(
        {"operation": "goto", "url": "http://example.com/redirect"}, ctx
    )
    assert "error" in result, result
    assert "SSRF" in result["error"]
    assert page.closed  # page closed so later reads cannot hit the internal URL
