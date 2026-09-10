"""browser_playwright upload + recording against a real headless Chromium.

The fake-page tests pin call shape; these pin the thing the customer actually
reported — that a file reaches the page. The page reads back name/size/content
from the ``<input type=file>`` so a buffer that never made it across the
CDP boundary fails loudly.

Skipped when Chromium is missing (CI installs no browsers), same convention
as test_browser_bbox.py.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from browser_fakes import chromium_available
from agent_core.tools.base import ToolContext
from agent_core.tools.browser_playwright import BrowserPlaywrightTool

requires_chromium = pytest.mark.skipif(
    not chromium_available(),
    reason="Playwright Chromium not installed (run: playwright install chromium)",
)

# The page reports what it received into #out, so the assertions can read real
# bytes rather than trust the tool's own echo. The chooser button has no
# visible input to target.
_PAGE = """<!DOCTYPE html>
<html><body>
<input id="plain" type="file" />
<button id="pick">Upload</button>
<pre id="out"></pre>
<script>
document.getElementById('pick').addEventListener('click', () => {
  const input = document.createElement('input');
  input.type = 'file';
  input.addEventListener('change', () => report(input.files[0]));
  input.click();
});
async function report(file) {
  const text = await file.text();
  document.getElementById('out').textContent =
    JSON.stringify({ name: file.name, size: file.size, type: file.type, text });
}
document.getElementById('plain').addEventListener('change', (e) => report(e.target.files[0]));
</script>
</body></html>
"""


@pytest.fixture(autouse=True)
def _ssrf_off(monkeypatch):
    """Loopback server: SSRF_ENABLED in the ambient environment would block it."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)


@pytest.fixture
def page_server(tmp_path):
    (tmp_path / "upload.html").write_text(_PAGE, encoding="utf-8")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
    )
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # `localhost`, not the loopback literal: the tool's URL gate blocks literal
    # IPs unconditionally but lets hostnames through when SSRF_ENABLED is off.
    yield f"http://localhost:{port}/upload.html"
    srv.shutdown()


def make_context(project_fs_path: str) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path=project_fs_path,
        conversation_id="conv",
        user_id="user-1",
    )


async def _uploaded(page_url: str, ctx: ToolContext, params: dict) -> dict:
    tool = BrowserPlaywrightTool()
    goto = await tool.execute({"operation": "goto", "url": page_url}, ctx)
    assert "error" not in goto, goto
    result = await tool.execute(params, ctx)
    assert "error" not in result, result
    text = await tool.execute({"operation": "get_text", "selector": "#out"}, ctx)
    import json

    return json.loads(text["text"])


@requires_chromium
async def test_inline_payload_reaches_the_input(page_server, tmp_path):
    ctx = make_context(str(tmp_path))
    received = await _uploaded(
        page_server,
        ctx,
        {
            "operation": "upload",
            "selector": "#plain",
            "files": [{"source": "inline", "name": "orders.csv", "content": "id,total\n1,42\n"}],
        },
    )
    assert received == {
        "name": "orders.csv",
        "size": 14,
        "type": "text/csv",
        "text": "id,total\n1,42\n",
    }


@requires_chromium
async def test_path_payload_reaches_the_input(page_server, tmp_path):
    fixture = tmp_path / "tests" / "fixtures" / "sample.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("from disk", encoding="utf-8")
    ctx = make_context(str(tmp_path))
    received = await _uploaded(
        page_server,
        ctx,
        {
            "operation": "upload",
            "selector": "#plain",
            "files": [{"source": "path", "path": "tests/fixtures/sample.txt"}],
        },
    )
    assert received["name"] == "sample.txt"
    assert received["text"] == "from disk"


@requires_chromium
async def test_chooser_upload_reaches_the_input(page_server, tmp_path):
    ctx = make_context(str(tmp_path))
    received = await _uploaded(
        page_server,
        ctx,
        {
            "operation": "upload",
            "selector": "#pick",
            "via_chooser": True,
            "files": [{"source": "inline", "name": "report.json", "content": '{"ok":true}'}],
        },
    )
    assert received["name"] == "report.json"
    assert received["type"] == "application/json"
    assert received["text"] == '{"ok":true}'


@requires_chromium
async def test_recording_produces_a_playable_webm(page_server, tmp_path):
    """record_stop must yield a non-trivial .webm — that requires Playwright's
    bundled ffmpeg to be present and the context to close before save_as."""
    tool = BrowserPlaywrightTool()
    ctx = make_context(str(tmp_path))
    await tool.execute({"operation": "goto", "url": page_server}, ctx)
    started = await tool.execute({"operation": "record_start"}, ctx)
    assert started.get("recording") is True, started

    # Whatever is recorded has to be long enough for ffmpeg to write frames.
    await tool.execute({"operation": "evaluate", "script": "new Promise(r => setTimeout(r, 1200))"}, ctx)
    await tool.execute({"operation": "click", "selector": "#pick"}, ctx)

    result = await tool.execute({"operation": "record_stop"}, ctx)
    assert result.get("success") is True, result
    assert result["size"] > 1024, result
    assert Path(result["path"]).exists()
    assert result["files"][0]["media_type"] == "video/webm"
