"""Tests for the shared media URL helper (_media.media_url)."""
from __future__ import annotations

import mimetypes
from unittest.mock import patch

from agent_core.tools._media import media_url, media_type_for, normalize_media_urls, sanitize_suffix
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
    assert media_type_for("photo.png") == "image/png"
    # The octet-stream fallback fires only when mimetypes knows nothing about
    # the suffix. Hosts disagree on what "unknown" means — Linux mime.types
    # registers .xyz as chemical/x-xyz while Python's builtin table does not —
    # so stub the lookup rather than pick a suffix that is merely unregistered
    # on this machine.
    with patch.object(mimetypes, "guess_type", return_value=(None, None)):
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


# --- normalize_media_urls --------------------------------------------------


def test_normalize_no_media_url_passthrough():
    assert normalize_media_urls("plain text with no links") == "plain text with no links"


def test_normalize_correct_url_passthrough():
    url = "https://app.example.com/agent/api/media/p/c/code_123.pptx"
    assert normalize_media_urls(f"Download: {url}") == f"Download: {url}"


def test_normalize_duplicated_base_collapses():
    bad = "https://app.example.com/agenthttps://app.example.com/agent/api/media/p/c/code_123.pptx"
    good = "https://app.example.com/agent/api/media/p/c/code_123.pptx"
    assert normalize_media_urls(f"here: {bad}") == f"here: {good}"


def test_normalize_duplicated_base_no_subpath_collapses():
    bad = "https://example.comhttps://example.com/api/media/p/c/f.txt"
    good = "https://example.com/api/media/p/c/f.txt"
    assert normalize_media_urls(bad) == good


def test_normalize_duplicated_base_in_sentence():
    text = (
        "PPT 下载：https://app.example.com/agent"
        "https://app.example.com/agent/api/media/p/c/code_123.pptx"
        "（也可在右侧下载）"
    )
    out = normalize_media_urls(text)
    assert "https://app.example.com/agent/api/media/p/c/code_123.pptx" in out
    assert out.count("https://app.example.com/agent") == 1


def test_normalize_multiple_urls():
    bad1 = "https://h.example.comhttps://h.example.com/api/media/p/c/a.pptx"
    bad2 = "https://h.example.comhttps://h.example.com/api/media/p/c/b.xlsx"
    out = normalize_media_urls(f"{bad1} and {bad2}")
    assert out == (
        "https://h.example.com/api/media/p/c/a.pptx and https://h.example.com/api/media/p/c/b.xlsx"
    )


def test_normalize_does_not_touch_nonmedia_duplicate():
    # A normal duplicated-looking prefix (e.g. a copied host) that does not
    # lead into /api/media/ is left alone — only media URLs are canonicalized.
    text = "see https://example.comhttps://example.com for details"
    assert normalize_media_urls(text) == text


def test_normalize_different_host_not_touched():
    # The second URL has a different host; no duplication to collapse.
    text = "https://a.example/api/media/p/c/f.txt and https://b.example/api/media/p/c/g.txt"
    assert normalize_media_urls(text) == text
