"""browser_playwright screen recording — context swap, finalization, cleanup."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_core.tools.browser_playwright import BrowserPlaywrightTool
from browser_fakes import FakeVideo, install_browser, make_context


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    install_browser(monkeypatch, context)
    return context


async def test_record_start_swaps_in_a_video_context(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "goto", "url": "http://site.test/form"}, ctx)
    first_context = ctx._browser_context

    result = await tool.execute({"operation": "record_start"}, ctx)

    assert result["recording"] is True, result
    assert first_context.closed is True
    new_context = ctx._browser_context
    assert new_context is not first_context
    video_dir = new_context.kwargs["record_video_dir"]
    assert Path(video_dir).is_dir()
    assert new_context.kwargs["record_video_size"] == {"width": 1280, "height": 720}
    # Cookies/localStorage survive the swap; the SSRF route is re-installed on
    # the new page or the recording session browses unguarded.
    assert first_context.storage_state_calls == 1
    assert new_context.kwargs["storage_state"] == first_context.storage_state_value
    assert ctx._browser_page.routes == ["**/*"]
    assert result["page_url"] == "http://site.test/form"
    assert ctx._browser_page.url == "http://site.test/form"


async def test_record_start_rejects_double_start(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)
    second = await tool.execute({"operation": "record_start"}, ctx)
    assert "Already recording" in second["error"]


async def test_record_stop_without_start_reports_cleanly(ctx):
    result = await BrowserPlaywrightTool().execute({"operation": "record_stop"}, ctx)
    assert "No active recording" in result["error"]


async def test_record_stop_saves_webm_into_conversation_media(ctx, tmp_path):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)
    tmp_dir = Path(ctx._browser_context.kwargs["record_video_dir"])
    video = ctx._browser_page.video

    result = await tool.execute({"operation": "record_stop"}, ctx)

    assert result["success"] is True, result
    assert result["filename"].endswith(".webm")
    assert Path(result["path"]).parent == tmp_path / ".tmp" / "media" / "conv"
    assert Path(result["path"]).exists()
    assert result["size"] == 2048
    assert result["files"][0]["media_type"] == "video/webm"
    assert result["files"][0]["url"].endswith(result["filename"])
    assert "warning" not in result
    # Temp tree and the browser-side copy are both released.
    assert not tmp_dir.exists()
    assert video.deleted is True
    # State is cleared, so the next operation lazily builds a plain context.
    assert ctx._browser_recording is None
    assert ctx._browser_context is None
    assert ctx._browser_page is None
    assert ctx._browser_contexts == []
    assert ctx._browser_pages == []


async def test_record_stop_names_the_recording_after_the_scenario(ctx, tmp_path):
    """The name is what a Jira reviewer sees, and nothing downstream can
    rename the file — so it has to be set here."""
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)

    result = await tool.execute(
        {"operation": "record_stop", "filename": "proj-456-import-orders"}, ctx
    )

    assert result["filename"] == "proj-456-import-orders.webm"
    assert result["path"].endswith("proj-456-import-orders.webm")
    # The playback URL is built from the filename, so it must survive the
    # media router's filename whitelist.
    assert re.fullmatch(r"[A-Za-z0-9_\-][A-Za-z0-9_\-.]*", result["filename"])


async def test_record_stop_sanitizes_a_hostile_filename(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)

    result = await tool.execute(
        {"operation": "record_stop", "filename": "../../etc/passwd 上传.wbmk"}, ctx
    )

    assert "/" not in result["filename"] and "\\" not in result["filename"]
    assert result["filename"].endswith(".webm")
    assert re.fullmatch(r"[A-Za-z0-9_\-][A-Za-z0-9_\-.]*", result["filename"])


async def test_record_stop_falls_back_to_a_generated_name(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)

    result = await tool.execute({"operation": "record_stop", "filename": "///"}, ctx)

    assert result["filename"].startswith("recording_")
    assert result["filename"].endswith(".webm")


async def test_record_stop_warns_about_a_too_short_recording(ctx, monkeypatch):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)
    ctx._browser_recording["video"] = FakeVideo(payload=b"webm")

    result = await tool.execute({"operation": "record_stop"}, ctx)

    assert result["size"] == 4
    assert "warning" in result


async def test_cleanup_closes_a_context_left_recording(ctx):
    tool = BrowserPlaywrightTool()
    await tool.execute({"operation": "record_start"}, ctx)
    recording_context = ctx._browser_context

    await ctx.cleanup()

    assert recording_context.closed is True
    assert ctx._browser_recording is None
