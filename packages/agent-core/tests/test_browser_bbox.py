"""Tests for browser_playwright bounding_box operation.

Regression for the #1 QA screenshot-annotation defect: marks landing at the
wrong place. ``bounding_box`` returns BOTH viewport-relative and full-page
(document) coordinates so the caller cannot mix coordinate systems; these
tests pin the exact JS math with a real headless Chromium against a local
HTML page (localhost hostname, not an SSRF target).

CI does not install Playwright browsers (agent-core job installs only
``pip install -e ".[test]"``), so the whole module is skipped when the
Chromium executable is missing — same convention as the repo's other
skipif markers.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.browser_playwright import BrowserPlaywrightTool


def _chromium_available() -> bool:
    """True when the Playwright Chromium binary is actually installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


requires_chromium = pytest.mark.skipif(
    not _chromium_available(),
    reason="Playwright Chromium not installed (run: playwright install chromium)",
)

_PAGE = """<!DOCTYPE html>
<html><head><style>
body { margin: 0; font-family: sans-serif; }
.spacer { height: 1200px; }
.target { position: absolute; left: 100px; top: 640px; width: 200px; height: 50px; background: #2563eb; }
</style></head>
<body>
<div class="target" id="t"></div>
<div class="spacer"></div>
</body></html>
"""


@pytest.fixture(autouse=True)
def _ssrf_off(monkeypatch):
    """This suite talks to a loopback server; SSRF_ENABLED in the ambient
    environment would block loopback URLs at goto."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)


@pytest.fixture
def page_server(tmp_path):
    (tmp_path / "bbox.html").write_text(_PAGE, encoding="utf-8")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    # `localhost` hostname, NOT the 127.0.0.1 literal: the browser tool's
    # URL gate blocks literal loopback IPs unconditionally, but hostnames
    # pass when SSRF_ENABLED is off (same convention as test_sandbox.py).
    yield f"http://localhost:{port}/bbox.html"
    srv.shutdown()


def make_context(project_fs_path: str) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=project_fs_path,
        conversation_id="conv",
        user_id="user-1",
    )


@requires_chromium
async def test_bounding_box_requires_selector(page_server, tmp_path):
    ctx = make_context(str(tmp_path))
    tool = BrowserPlaywrightTool()
    goto = await tool.execute({"operation": "goto", "url": page_server}, ctx)
    assert "error" not in goto, goto
    result = await tool.execute({"operation": "bounding_box"}, ctx)
    assert "error" in result
    assert "selector" in result["error"]


@requires_chromium
async def test_bounding_box_reports_viewport_and_fullpage(page_server, tmp_path):
    ctx = make_context(str(tmp_path))
    tool = BrowserPlaywrightTool()
    goto = await tool.execute({"operation": "goto", "url": page_server}, ctx)
    assert "error" not in goto, goto
    # scroll 300px so viewport and document coords differ
    await tool.execute(
        {"operation": "evaluate", "script": "window.scrollTo(0, 300)"}, ctx
    )
    result = await tool.execute({"operation": "bounding_box", "selector": ".target"}, ctx)
    assert "error" not in result, result
    box = result["bounding_box"]

    # CSS-absolute element: left=100, top=640, w=200, h=50 in BOTH systems
    assert box["fullPage"] == {"x": 100, "y": 640, "width": 200, "height": 50}
    # viewport y = 640 - scrollY(300) = 340
    assert box["viewport"]["y"] == 340
    assert box["scrollY"] == 300
    # both share size and x (no horizontal scroll)
    assert box["viewport"]["width"] == 200
    assert box["viewport"]["x"] == 100


@requires_chromium
async def test_bounding_box_unknown_selector_fails_cleanly(page_server, tmp_path):
    ctx = make_context(str(tmp_path))
    tool = BrowserPlaywrightTool()
    goto = await tool.execute({"operation": "goto", "url": page_server}, ctx)
    assert "error" not in goto, goto
    result = await tool.execute(
        {"operation": "bounding_box", "selector": ".nope", "timeout": 2000}, ctx
    )
    assert "error" in result
    assert ".nope" in result["error"]
