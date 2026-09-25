"""Proxy resolution shared by the browser tool and code_executor.

The browser reads the proxy once at Chromium launch; code_executor passes the
same URL into the sandbox environment. These tests pin the precedence, the
normalization of inherited spellings, and the credential redaction — all hosts
are example.com-family fakes.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from agent_core.tools import code_executor as code_executor_module
from agent_core.tools.base import ToolContext

_PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
_HOST_PROXY = "socks5://host-proxy.example.com:1080"
_CONFIGURED = "http://configured.example.com:8080"


def make_context(*, settings: dict[str, str] | None = None) -> ToolContext:
    ctx = ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
        db_session=None,
    )
    if settings:
        ctx.integration_settings = settings
    return ctx


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    """Drop whatever proxy the developer's shell exports."""
    for key in (*_PROXY_KEYS, "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# proxy_url()
# ---------------------------------------------------------------------------


def test_proxy_url_precedence_matches_browser(monkeypatch):
    """Same ladder as the browser launch — one rung at a time.

    The uppercase rungs are exercised through injected settings and with the
    lowercase spellings cleared: os.environ is case-insensitive on Windows, so
    an uppercase lookup there also answers for a lowercase variable.
    """
    ctx = make_context()
    assert ctx.proxy_url() == ""

    monkeypatch.setenv("http_proxy", "http://lower-http.example.com:1")
    assert ctx.proxy_url() == "http://lower-http.example.com:1"

    monkeypatch.setenv("https_proxy", "http://lower-https.example.com:2")
    assert ctx.proxy_url() == "http://lower-https.example.com:2"

    monkeypatch.delenv("http_proxy")
    monkeypatch.delenv("https_proxy")
    ctx = make_context(settings={"HTTP_PROXY": "http://upper-http.example.com:3"})
    assert ctx.proxy_url() == "http://upper-http.example.com:3"

    ctx = make_context(settings={"HTTPS_PROXY": "http://upper-https.example.com:4"})
    assert ctx.proxy_url() == "http://upper-https.example.com:4"


def test_proxy_url_reads_uppercase_process_env(monkeypatch):
    """With nothing lowercase set, the cfg() fallback reaches the exported
    HTTPS_PROXY that main.py installs at startup."""
    monkeypatch.setenv("HTTPS_PROXY", "http://upper-https.example.com:4")
    assert make_context().proxy_url() == "http://upper-https.example.com:4"


@pytest.mark.skipif(sys.platform == "win32", reason="os.environ is case-insensitive on Windows")
def test_proxy_url_http_proxy_rung_beats_lowercase_https(monkeypatch):
    """HTTP_PROXY sits above both lowercase spellings in the browser's ladder."""
    monkeypatch.setenv("https_proxy", "http://lower-https.example.com:2")
    ctx = make_context(settings={"HTTP_PROXY": "http://upper-http.example.com:3"})
    assert ctx.proxy_url() == "http://upper-http.example.com:3"


def test_proxy_url_settings_beat_process_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://upper-https.example.com:4")
    ctx = make_context(settings={"HTTPS_PROXY": _CONFIGURED})
    assert ctx.proxy_url() == _CONFIGURED


# ---------------------------------------------------------------------------
# proxy_env()
# ---------------------------------------------------------------------------


def test_proxy_env_sets_all_spellings_and_drops_all_proxy():
    ctx = make_context(settings={"HTTPS_PROXY": _CONFIGURED})
    env = {"ALL_PROXY": _HOST_PROXY, "all_proxy": _HOST_PROXY, "NO_PROXY": "localhost"}
    assert ctx.proxy_env(env) is env
    assert [env[key] for key in _PROXY_KEYS] == [_CONFIGURED] * 4
    assert "ALL_PROXY" not in env and "all_proxy" not in env
    assert env["NO_PROXY"] == "localhost"


def test_proxy_env_without_proxy_scrubs_placeholders_and_keeps_no_proxy():
    """An empty .env placeholder must not reach the child, and the bypass list
    (main.py merges the LLM hosts into it) must survive."""
    ctx = make_context()
    env = {key: "" for key in _PROXY_KEYS}
    env.update({"ALL_PROXY": _HOST_PROXY, "NO_PROXY": "localhost,127.0.0.1"})
    ctx.proxy_env(env)
    assert [key for key in _PROXY_KEYS if key in env] == []
    assert "ALL_PROXY" not in env
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


# ---------------------------------------------------------------------------
# Context-less callers (script runs, git subprocesses, ensure_http_client)
# ---------------------------------------------------------------------------


def test_apply_proxy_env_resolves_from_process_env(monkeypatch):
    """Script runs and git subprocesses have no ToolContext — they normalize
    the four spellings from the process environment alone, with the same
    ALL_PROXY drop and NO_PROXY keep as the context path."""
    from agent_core.tools.base import apply_proxy_env, resolve_proxy_url

    monkeypatch.setenv("https_proxy", "http://lower.example.com:8080")
    monkeypatch.setenv("ALL_PROXY", _HOST_PROXY)
    env = {"ALL_PROXY": _HOST_PROXY, "NO_PROXY": "localhost"}

    assert resolve_proxy_url() == "http://lower.example.com:8080"
    assert apply_proxy_env(env) is env
    assert [env[key] for key in _PROXY_KEYS] == ["http://lower.example.com:8080"] * 4
    assert "ALL_PROXY" not in env
    assert env["NO_PROXY"] == "localhost"


def test_script_sandbox_env_normalizes_proxy(monkeypatch):
    """QA Playwright runs and scheduler script targets build their env here —
    a stale inherited ALL_PROXY or an empty placeholder must not leak through."""
    from agent_core.scripts.runner import sandbox_env

    monkeypatch.setenv("https_proxy", "http://lower.example.com:8080")
    monkeypatch.setenv("ALL_PROXY", _HOST_PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    env = sandbox_env()

    assert [env[key] for key in _PROXY_KEYS] == ["http://lower.example.com:8080"] * 4
    assert "ALL_PROXY" not in env and "all_proxy" not in env
    assert env["NO_PROXY"] == "localhost,127.0.0.1"
    assert "PATH" in env  # still a usable subprocess environment


def test_ensure_http_client_falls_to_the_http_proxy_rung(monkeypatch):
    """The Python HTTP tools used to read only HTTPS_PROXY; a deployment that
    sets only HTTP_PROXY proxied the shell and browser but not web_fetch /
    api_request — every rung of the shared ladder must apply here too."""
    from agent_core.tools import _http

    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    ctx = make_context(settings={"HTTP_PROXY": "http://http-rung.example.com:8080"})
    captured: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(_http.httpx, "AsyncClient", _Recorder)

    client = _http.ensure_http_client(ctx, "https://target.example.com/")

    assert client is not None
    assert captured.get("proxy") == "http://http-rung.example.com:8080"


# ---------------------------------------------------------------------------
# redact_proxy_credentials()
# ---------------------------------------------------------------------------


def test_redact_proxy_credentials_masks_url_and_password():
    url = "http://scanner:not-a-real-secret@proxy.example.com:8080"
    text = f"proxy error: cannot connect to {url} (auth not-a-real-secret)"
    out = code_executor_module.redact_proxy_credentials(text, url)
    assert "not-a-real-secret" not in out
    assert url not in out
    assert "[REDACTED:PROXY_URL]" in out


def test_redact_proxy_credentials_masks_percent_encoded_forms():
    url = "http://scanner:p%40ssw0rd@proxy.example.com:8080"
    text = f"{url} and decoded p@ssw0rd"
    out = code_executor_module.redact_proxy_credentials(text, url)
    assert "p%40ssw0rd" not in out
    assert "p@ssw0rd" not in out


def test_redact_proxy_credentials_masks_scheme_less_url():
    """A URL without a scheme parses as scheme="user" — the redactor must not
    rely on urlsplit for the userinfo."""
    url = "scanner:not-a-real-secret@proxy.example.com:8080"
    out = code_executor_module.redact_proxy_credentials(f"via {url}", url)
    assert "not-a-real-secret" not in out


def test_redact_proxy_credentials_without_credentials_is_noop():
    text = "http://proxy.example.com:8080 refused the connection"
    assert code_executor_module.redact_proxy_credentials(text, "http://proxy.example.com:8080") == text
    assert code_executor_module.redact_proxy_credentials(text, "") == text


# ---------------------------------------------------------------------------
# ensure_browser() launch kwargs
# ---------------------------------------------------------------------------


class _FakePlaywright:
    def __init__(self, launched: dict):
        self.chromium = SimpleNamespace(launch=self._launch)
        self._launched = launched

    async def _launch(self, **kwargs):
        self._launched.update(kwargs)
        return SimpleNamespace(close=self._noop)

    async def _noop(self):
        pass

    async def stop(self):
        pass


def _fake_playwright_module(launched: dict):
    class _Manager:
        async def start(self):
            return _FakePlaywright(launched)

    return SimpleNamespace(async_playwright=lambda: _Manager())


async def test_ensure_browser_passes_configured_proxy(monkeypatch):
    launched: dict = {}
    monkeypatch.setitem(sys.modules, "playwright.async_api", _fake_playwright_module(launched))
    ctx = make_context(settings={"HTTPS_PROXY": _CONFIGURED})
    await ctx.ensure_browser()
    assert launched["proxy"] == {"server": _CONFIGURED}
    await ctx.cleanup()


async def test_ensure_browser_omits_proxy_without_config(monkeypatch):
    launched: dict = {}
    monkeypatch.setitem(sys.modules, "playwright.async_api", _fake_playwright_module(launched))
    await make_context().ensure_browser()
    assert "proxy" not in launched
