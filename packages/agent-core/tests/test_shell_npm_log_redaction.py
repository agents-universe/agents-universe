"""ensure_node_deps must scrub the proxy credential out of npm's stderr.

npm quotes the proxy URL it could not reach — the shell tool's return site
even documents this — and both callers hand over an env that carries the
credentialed URL: shell._build_env runs proxy_env(), and the API script
executor's sandbox_env() keeps HTTPS_PROXY (the deny-list suffix is URL, not
PROXY). The raw stderr was still written to _log.warning and returned
verbatim, so the credential reached the server log (both paths) and the
stored run log via scripts.py's fail(f"Dependency setup failed: ...").
"""
from __future__ import annotations

import logging

from agent_core.tools import shell as shell_mod
from agent_core.tools.shell import ensure_node_deps

PROXY = "http://user:sup3rs3cret@proxy.example.com:1080"
STDERR = (
    b"npm error network request failed: request to "
    + PROXY.encode()
    + b" failed, reason: connect ECONNREFUSED"
)


class _FakeProc:
    def __init__(self, code: int, stderr: bytes) -> None:
        self.returncode = code
        self._stderr = stderr

    async def communicate(self, *args, **kwargs):
        return b"", self._stderr

    async def wait(self):
        return self.returncode


def _install_failure(monkeypatch, responses):
    """Make every npm subprocess call consume ``responses`` in order."""
    calls = {"n": 0}

    async def _fake(*args, **kwargs):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        code, err = responses[i]
        return _FakeProc(code, err)

    monkeypatch.setattr(shell_mod.asyncio, "create_subprocess_exec", _fake)
    monkeypatch.setattr(shell_mod.asyncio, "create_subprocess_shell", _fake)


def _pkg(tmp_path):
    pkg = tmp_path / "proj"
    pkg.mkdir()
    # typescript is one of the three packages whose bin shims
    # _required_node_bins demands, so a "successful" install that leaves
    # node_modules absent still triggers the npm rebuild path.
    (pkg / "package.json").write_text(
        '{"name": "t", "version": "0.0.0", "devDependencies": {"typescript": "^5.0.0"}}'
    )
    return pkg


async def test_install_failure_log_is_redacted(tmp_path, caplog, monkeypatch):
    _install_failure(monkeypatch, [(1, STDERR)])
    pkg = _pkg(tmp_path)
    env = {"PATH": "/usr/bin", "HTTPS_PROXY": PROXY}

    with caplog.at_level(logging.WARNING, logger="agent_core.tools.shell"):
        err = await ensure_node_deps("npm run build", str(pkg), str(tmp_path), env)

    assert err is not None
    assert "sup3rs3cret" not in caplog.text, "proxy credential written to the server log"
    assert "[REDACTED" in caplog.text, caplog.text
    # Returned text feeds both the shell error dict and the API run log.
    assert "sup3rs3cret" not in err, "proxy credential returned to the caller"
    assert "[REDACTED" in err


async def test_rebuild_failure_log_is_redacted(tmp_path, caplog, monkeypatch):
    # Install "succeeds" but the required bin shims are still missing, so
    # npm rebuild runs and fails — its stderr is the second log site.
    _install_failure(monkeypatch, [(0, b""), (1, STDERR)])
    pkg = _pkg(tmp_path)
    env = {"PATH": "/usr/bin", "HTTPS_PROXY": PROXY}

    with caplog.at_level(logging.WARNING, logger="agent_core.tools.shell"):
        err = await ensure_node_deps("npm run build", str(pkg), str(tmp_path), env)

    assert err is not None  # bins still missing → setup error returned
    assert "sup3rs3cret" not in caplog.text, "proxy credential written to the server log"
    assert "[REDACTED" in caplog.text, caplog.text


async def test_install_without_proxy_env_is_untouched(tmp_path, monkeypatch):
    # No credentialed URL in env → the message survives unredacted; the
    # scrub must not garble ordinary failures.
    _install_failure(monkeypatch, [(1, b"npm error code E404")])
    pkg = _pkg(tmp_path)

    err = await ensure_node_deps(
        "npm run build", str(pkg), str(tmp_path), {"PATH": "/usr/bin"}
    )
    assert err is not None
    assert "npm error code E404" in err
