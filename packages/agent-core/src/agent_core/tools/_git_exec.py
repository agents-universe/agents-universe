"""Shared git subprocess runner for the git-backed tools.

Extracted from git_repo so that every tool shelling out to git — including the
ones talking to remotes outside the configured host (skill_source) — goes
through one hardened path: credential injection via a temp askpass helper,
kill-on-timeout, output sanitization/truncation, and an environment with the
server's own credentials stripped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .base import ToolContext

_log = logging.getLogger(__name__)
_TIMEOUT_CLONE = 300
_TIMEOUT_PULL = 120
_TIMEOUT_DEFAULT = 30
_MAX_OUTPUT = 20_000


def _find_git() -> str | None:
    if found := shutil.which("git"):
        return found
    if sys.platform != "win32":
        return None
    roots = {
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData"),
    }
    for root in roots - {None}:
        for relative in ("Git/cmd/git.exe", "Git/bin/git.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return None


_GIT_BIN = _find_git()


def _dependency_missing(message: str) -> dict[str, Any]:
    return {"error": "dependency_missing", "dependency": "git", "message": message}


def _sanitize_output(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token and text else text


def _rmtree_force(path: Path) -> None:
    """Remove a directory tree, clearing read-only attributes first on win32.

    git loose objects are created with the read-only attribute on Windows, and
    shutil.rmtree then fails with WinError 5 on them (the clone rollback masks
    this with ignore_errors). Clear the attribute everywhere first so a genuine
    failure (e.g. an antivirus file lock) still surfaces as remove_failed.
    """
    if sys.platform == "win32":
        for root, _dirs, files in os.walk(path):
            for name in files + _dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
                except OSError:
                    pass
    shutil.rmtree(path)


def _safe_git_env() -> dict[str, str]:
    """os.environ with credential-like keys stripped for the git subprocess.

    Git hooks (post-commit, pre-push, ...) execute with this environment, so
    DB passwords / API keys in the server env must not reach them. Mirrors
    ToolContext.safe_env() filtering; GIT_ASKPASS_TOKEN is re-added by the
    caller after filtering.
    """
    deny_suffixes = ToolContext._ENV_DENY_SUFFIXES
    deny_prefixes = ToolContext._ENV_DENY_PREFIXES
    deny_exact = ToolContext._ENV_DENY_EXACT
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in deny_exact:
            continue
        if upper.rsplit("_", 1)[-1] in deny_suffixes:
            continue
        if any(upper.startswith(p) for p in deny_prefixes):
            continue
        env[key] = value
    return env


async def run_git(
    args: list[str],
    cwd: str | Path,
    timeout: int = _TIMEOUT_DEFAULT,
    token: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run git with credentials injected through a throwaway askpass helper.

    ``env`` replaces the default filtered os.environ — callers that need extra
    hardening variables pass their own copy. The token is never placed on the
    command line or in the URL; it only ever lives in the helper's environment.
    """
    if not _GIT_BIN:
        return _dependency_missing("Git executable was not found")
    env = dict(env) if env is not None else _safe_git_env()
    askpass: Path | None = None
    # Without this an anonymous clone of a repository that wants credentials
    # blocks forever on the terminal prompt instead of failing.
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        suffix = ".cmd" if sys.platform == "win32" else ".sh"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, prefix="git-askpass-", delete=False,
                encoding="utf-8", newline="",
            ) as helper:
                askpass = Path(helper.name)
                if sys.platform == "win32":
                    helper.write(
                        '@echo off\r\necho %~1 | findstr /I "password" >nul\r\n'
                        'if not errorlevel 1 (echo %GIT_ASKPASS_TOKEN%) else (echo oauth2)\r\n'
                    )
                else:
                    helper.write(
                        '#!/bin/sh\ncase "$1" in\n'
                        '  *[Pp]assword*) printf \'%s\\n\' "$GIT_ASKPASS_TOKEN" ;;\n'
                        '  *) printf \'oauth2\\n\' ;;\nesac\n'
                    )
            if sys.platform != "win32":
                askpass.chmod(0o700)
        except OSError as exc:
            return _dependency_missing(f"Could not create Git credential helper: {exc}")
        env.update(
            GIT_ASKPASS=str(askpass),
            GIT_ASKPASS_TOKEN=token,
        )
    # process is pre-declared: a cancellation landing between the await of
    # create_subprocess_exec and its assignment would otherwise hit a
    # NameError in the CancelledError handler below (same pattern as
    # code_executor.py / shell.py).
    process: asyncio.subprocess.Process | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                _GIT_BIN, *args, cwd=str(cwd), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if process is not None:
                process.kill()
                await process.wait()
            return {"error": f"Git command timed out after {timeout}s"}
        except asyncio.CancelledError:
            if process is not None:
                process.kill()
                await process.wait()
            raise
        except OSError as exc:
            return _dependency_missing(f"Git could not be started: {exc}")
        out = _sanitize_output(stdout.decode(errors="replace"), token)[:_MAX_OUTPUT]
        err = _sanitize_output(stderr.decode(errors="replace"), token)[:_MAX_OUTPUT]
        if process.returncode:
            _log.warning("git %s failed: %s", args[0] if args else "?", err[:500])
            return {
                "error": f"Git exited with code {process.returncode}",
                "exit_code": process.returncode,
                "stdout": out,
                "stderr": err,
            }
        return {"stdout": out, "stderr": err, "exit_code": 0}
    finally:
        if askpass:
            try:
                askpass.unlink(missing_ok=True)
            except OSError:
                pass
