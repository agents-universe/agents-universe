"""browser_playwright upload operation — payload sources and chooser mode."""
from __future__ import annotations

import base64

import pytest

from agent_core.tools.browser_playwright import BrowserPlaywrightTool
from browser_fakes import install_browser, make_context


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    attachments = {"attached_ab12.csv": b"id,total\n1,42\n"}
    context = make_context(
        tmp_path,
        upload_file_lookup=attachments.get,
        upload_file_names=lambda: list(attachments),
    )
    install_browser(monkeypatch, context)
    return context


async def test_upload_inline_content(ctx):
    result = await BrowserPlaywrightTool().execute(
        {
            "operation": "upload",
            "selector": "input[type=file]",
            "files": [
                {
                    "source": "inline",
                    "name": "orders.csv",
                    "mime_type": "text/csv",
                    "content": "id,total\n1,42\n",
                }
            ],
        },
        ctx,
    )
    assert result["success"] is True, result
    selector, files = ctx._browser_page.uploaded[-1]
    assert selector == "input[type=file]"
    assert files == [{"name": "orders.csv", "mimeType": "text/csv", "buffer": b"id,total\n1,42\n"}]
    assert result["uploaded"] == [
        {"name": "orders.csv", "size": 14, "mime_type": "text/csv", "source": "inline"}
    ]


async def test_upload_inline_base64_binary(ctx):
    raw = b"\x89PNG\r\n\x1a\n"
    await BrowserPlaywrightTool().execute(
        {
            "operation": "upload",
            "selector": "#avatar",
            "files": [
                {
                    "source": "inline",
                    "name": "avatar.png",
                    "content_base64": base64.b64encode(raw).decode(),
                }
            ],
        },
        ctx,
    )
    _, files = ctx._browser_page.uploaded[-1]
    assert files[0]["buffer"] == raw
    assert files[0]["mimeType"] == "image/png"


async def test_upload_from_chat_attachment(ctx):
    result = await BrowserPlaywrightTool().execute(
        {"operation": "upload", "selector": "#file", "files": [{"source": "attachment", "name": "attached_ab12.csv"}]},
        ctx,
    )
    assert result["uploaded"][0]["source"] == "attachment"
    _, files = ctx._browser_page.uploaded[-1]
    assert files[0]["buffer"] == b"id,total\n1,42\n"


async def test_upload_from_workspace_path(ctx, tmp_path):
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "seed.json").write_bytes(b'{"a":1}')
    result = await BrowserPlaywrightTool().execute(
        {"operation": "upload", "selector": "#file", "files": [{"source": "path", "path": "tests/fixtures/seed.json"}]},
        ctx,
    )
    assert result["uploaded"][0]["source"] == "path"
    _, files = ctx._browser_page.uploaded[-1]
    assert files[0]["name"] == "seed.json"
    assert files[0]["mimeType"] == "application/json"


async def test_upload_via_chooser_clicks_then_sets_files(ctx):
    result = await BrowserPlaywrightTool().execute(
        {
            "operation": "upload",
            "selector": "button.upload",
            "via_chooser": True,
            "files": [{"source": "inline", "name": "a.txt", "content": "x"}],
        },
        ctx,
    )
    page = ctx._browser_page
    assert result["via_chooser"] is True
    assert page.chooser_expectations == 1
    assert page.clicks == ["button.upload"]
    assert page.file_chooser.set_calls == 1
    assert page.file_chooser.files[0]["name"] == "a.txt"
    assert page.uploaded == []  # no direct input call in chooser mode


async def test_upload_result_never_uses_the_files_key(ctx):
    """A result["files"] entry with a url becomes a user-facing deliverable."""
    result = await BrowserPlaywrightTool().execute(
        {
            "operation": "upload",
            "selector": "#f",
            "files": [{"source": "inline", "name": "a.txt", "content": "x"}],
        },
        ctx,
    )
    assert "files" not in result
    assert "uploaded" in result


async def test_upload_argument_and_source_errors(ctx):
    tool = BrowserPlaywrightTool()
    missing_files = await tool.execute({"operation": "upload", "selector": "#f"}, ctx)
    assert "requires 'files'" in missing_files["error"]

    missing_selector = await tool.execute(
        {"operation": "upload", "files": [{"source": "inline", "name": "a.txt", "content": "x"}]},
        ctx,
    )
    assert "requires 'selector'" in missing_selector["error"]

    hidden = await tool.execute(
        {"operation": "upload", "selector": "#f", "files": [{"source": "path", "path": ".env"}]},
        ctx,
    )
    assert "Hidden files" in hidden["error"]

    unknown_attachment = await tool.execute(
        {"operation": "upload", "selector": "#f", "files": [{"source": "attachment", "name": "nope.csv"}]},
        ctx,
    )
    assert "not found" in unknown_attachment["error"]
    assert ctx._browser_page.uploaded == []  # nothing was pushed on any failure
