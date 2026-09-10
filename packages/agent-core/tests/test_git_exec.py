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
