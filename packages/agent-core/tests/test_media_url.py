"""Tests for the shared media URL helper (_media.media_url)."""
from __future__ import annotations

from agent_core.tools._media import media_url, media_type_for, sanitize_suffix
from agent_core.tools.base import ToolContext


def make_context(**overrides) -> ToolContext:
    kwargs = dict(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )
    kwargs.update(overrides)
    return ToolContext(**kwargs)


def test_sanitize_suffix():
    assert sanitize_suffix("a.PNG") == ".png"
    assert sanitize_suffix("noext") == ""
    assert sanitize_suffix("weird.txt.exe") == ".exe"
    assert sanitize_suffix("x." + "a" * 20) == ""


def test_media_type_for():
    assert media_type_for("report.csv") == "text/csv"
    assert media_type_for("doc.pdf") == "application/pdf"
    assert media_type_for("unknown.xyz") == "application/octet-stream"


def test_media_url_relative_fallback_when_no_base():
    ctx = make_context()
    url = media_url(ctx, "code_abcd1234.html")
    assert url == "/api/media/proj/conv/code_abcd1234.html"


def test_media_url_absolute_with_base():
    ctx = make_context(app_base_url="https://agent.agents-universe.com")
    url = media_url(ctx, "code_abcd1234.html")
    assert url == "https://agent.agents-universe.com/api/media/proj/conv/code_abcd1234.html"


def test_media_url_absolute_with_base_and_root_path():
    ctx = make_context(
        app_base_url="https://app.example.com",
        app_root_path="/agent",
    )
    url = media_url(ctx, "code_abcd1234.html")
    assert url == "https://app.example.com/agent/api/media/proj/conv/code_abcd1234.html"


def test_media_url_base_already_contains_root_path():
    # APP_BASE_URL already carries the sub-path; APP_ROOT_PATH must not double it.
    ctx = make_context(
        app_base_url="https://app.example.com/agent",
        app_root_path="/agent",
    )
    url = media_url(ctx, "code_abcd1234.html")
    assert url == "https://app.example.com/agent/api/media/proj/conv/code_abcd1234.html"


def test_media_url_base_trailing_slash_is_normalized():
    ctx = make_context(app_base_url="https://example.com/")
    url = media_url(ctx, "f.txt")
    assert url == "https://example.com/api/media/proj/conv/f.txt"


def test_media_url_root_path_without_leading_slash():
    ctx = make_context(
        app_base_url="https://example.com",
        app_root_path="agent",
    )
    url = media_url(ctx, "f.txt")
    assert url == "https://example.com/agent/api/media/proj/conv/f.txt"


def test_media_url_ignores_non_http_base():
    ctx = make_context(app_base_url="localhost:8000")
    url = media_url(ctx, "f.txt")
    assert url == "/api/media/proj/conv/f.txt"
