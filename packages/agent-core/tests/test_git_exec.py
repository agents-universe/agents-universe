"""Shared git runner: credential handling, env overrides, output sanitization."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import agent_core.tools._git_exec as _git_exec
from agent_core.tools._git_exec import _safe_git_env, _sanitize_output, run_git

pytestmark = pytest.mark.skipif(
    _git_exec._GIT_BIN is None, reason="git executable not available"
)

_CLONE = ["clone", "https://git.example.com/acme/x.git"]


@pytest.fixture
def capture_env(monkeypatch):
    """Intercept the subprocess launch and hand back the environment it saw."""
    seen: dict[str, str] = {}

    async def _fake_exec(*args, **kwargs):
        seen.update(kwargs["env"])
        # A real helper script is not executable yet — read it back instead.
        helper = kwargs["env"].get("GIT_ASKPASS")
        if helper:
            seen["_askpass_script"] = Path(helper).read_text(encoding="utf-8")
            seen["_askpass_path"] = helper
        raise FileNotFoundError("subprocess launch intercepted")

    monkeypatch.setattr(_git_exec.asyncio, "create_subprocess_exec", _fake_exec)
    return seen


@pytest.mark.asyncio
async def test_terminal_prompt_is_disabled_without_a_token(tmp_path, capture_env):
    """An anonymous clone of a private repo must fail, not hang on a prompt."""
    await run_git(_CLONE, tmp_path)

    assert capture_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in capture_env


@pytest.mark.asyncio
async def test_token_goes_through_a_temp_askpass_that_is_cleaned_up(tmp_path, capture_env):
    result = await run_git(_CLONE, tmp_path, token="ghp-supersecret")

    assert result["error"] == "dependency_missing"
    assert capture_env["GIT_ASKPASS_TOKEN"] == "ghp-supersecret"
    # The helper reads the token from the environment: no copy of it is ever
    # written to disk, and the throwaway script is gone once the call returns.
    assert "ghp-supersecret" not in capture_env["_askpass_script"]
    assert not Path(capture_env["_askpass_path"]).exists()


@pytest.mark.asyncio
async def test_windows_helper_answers_the_password_prompt(tmp_path, capture_env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    await run_git(_CLONE, tmp_path, token="t0k3n")

    assert capture_env["_askpass_path"].endswith(".cmd")
    assert "%GIT_ASKPASS_TOKEN%" in capture_env["_askpass_script"]


@pytest.mark.asyncio
async def test_askpass_helper_is_unlinked_when_its_write_fails(tmp_path, monkeypatch):
    """NamedTemporaryFile(delete=False) leaves the file behind, and the only
    unlink runs in the finally around the subprocess — an OSError while
    writing/chmod'ing the helper returns early, leaking the temp file on
    every failure."""
    import tempfile as _tempfile_mod

    real_ntf = _tempfile_mod.NamedTemporaryFile
    created: list[str] = []

    def _failing_ntf(*args, **kwargs):
        real = real_ntf(*args, **kwargs)
        created.append(real.name)

        class _FailingHelper:
            name = real.name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                real.close()
                return False

            def write(self, *_a, **_k):
                raise OSError("disk full")

        return _FailingHelper()

    monkeypatch.setattr(_git_exec.tempfile, "NamedTemporaryFile", _failing_ntf)

    result = await run_git(_CLONE, tmp_path, token="ghp-x")

    assert result["error"] == "dependency_missing"
    assert created, "helper file was never created"
    assert not Path(created[0]).exists(), "askpass helper leaked on the OSError path"


@pytest.mark.asyncio
async def test_env_argument_replaces_the_default_environment(tmp_path, capture_env):
    await run_git(["status"], tmp_path, env={"PATH": "/usr/bin", "GIT_ALLOW_PROTOCOL": "https"})

    assert capture_env["GIT_ALLOW_PROTOCOL"] == "https"
    assert capture_env["GIT_TERMINAL_PROMPT"] == "0"


def test_safe_git_env_strips_credential_like_keys(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-1")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _safe_git_env()

    assert "DB_PASSWORD" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


@pytest.mark.asyncio
async def test_default_env_normalizes_proxy_spellings(tmp_path, capture_env, monkeypatch):
    """git's libcurl reads ALL_PROXY too — an inherited value the platform
    never resolved would route git somewhere the browser tool never goes,
    and an empty placeholder breaks its URI parsing."""
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy.example.com:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://stale.example.com:1080")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    await run_git(["status"], tmp_path)

    assert capture_env["HTTPS_PROXY"] == "http://host-proxy.example.com:8080"
    assert capture_env["HTTP_PROXY"] == "http://host-proxy.example.com:8080"
    assert capture_env["https_proxy"] == "http://host-proxy.example.com:8080"
    assert capture_env["http_proxy"] == "http://host-proxy.example.com:8080"
    assert "ALL_PROXY" not in capture_env and "all_proxy" not in capture_env
    assert capture_env["NO_PROXY"] == "localhost,127.0.0.1"


@pytest.mark.asyncio
async def test_caller_supplied_env_is_not_renormalized(tmp_path, capture_env, monkeypatch):
    """skill_source hands run_git an env already normalized through
    ToolContext.proxy_env — re-normalizing could replace the settings-resolved
    proxy with a different process-env one."""
    monkeypatch.setenv("https_proxy", "http://env-proxy.example.com:8080")
    custom = {"PATH": "/usr/bin", "HTTPS_PROXY": "http://caller-proxy.example.com:8080"}

    await run_git(["status"], tmp_path, env=custom)

    assert capture_env["HTTPS_PROXY"] == "http://caller-proxy.example.com:8080"
    assert capture_env["PATH"] == "/usr/bin"


def test_sanitize_output_masks_only_the_token():
    assert _sanitize_output("fatal: bad creds abc123", "abc123") == "fatal: bad creds ***"
    assert _sanitize_output("nothing to mask", None) == "nothing to mask"
    assert _sanitize_output("", "abc123") == ""


@pytest.mark.asyncio
async def test_run_git_reports_a_nonzero_exit_with_stderr(tmp_path):
    await run_git(["init", "--initial-branch=main"], tmp_path)

    result = await run_git(["rev-parse", "nope"], tmp_path)

    assert result["exit_code"] != 0
    assert result["stderr"]


@pytest.mark.asyncio
async def test_run_git_captures_stdout_on_success(tmp_path):
    await run_git(["init", "--initial-branch=main"], tmp_path)

    result = await run_git(["rev-parse", "--is-inside-work-tree"], tmp_path)

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "true"
