"""WebSocket auth for script-run logs under AUTH_BYPASS deployments.

Regression guard for `script_run_ws`: in bypass mode there is no session
cookie at all, so the endpoint must accept the run through the bypass user
instead of closing with 4001 (which uvicorn answers as HTTP 403 and the
script-run log pane sees as a dropped "connection lost" even though the
run is executing fine server-side).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers.scripts import script_run_ws


class _FakeSession:
    """Async context manager exposing execute() that returns our run row."""

    def __init__(self, run_row):
        self._run_row = run_row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._run_row
        return result


def _fake_run_row():
    row = MagicMock()
    row.status = "completed"
    row.stdout_log = "hello"
    row.stderr_log = ""
    row.exit_code = 0
    return row


def _fake_ws():
    ws = MagicMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive = AsyncMock(return_value={"type": "websocket.disconnect"})
    return ws


@pytest.mark.asyncio
async def test_script_run_ws_bypass_mode_accepts_without_cookie(monkeypatch):
    """AUTH_BYPASS on: no cookie -> still accept() and stream the done event."""
    from api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_bypass_enabled", True)
    monkeypatch.setattr(settings, "auth_bypass_user_id", "dev-user")
    monkeypatch.setattr("api.config.get_settings", lambda: settings)
    monkeypatch.setattr("api.database.AsyncSessionLocal", lambda: _FakeSession(_fake_run_row()))

    ws = _fake_ws()
    await script_run_ws("run-123", ws)

    ws.accept.assert_awaited_once()
    # The normal end-of-run close (no explicit code = 1000). The bug being
    # guarded is a pre-accept close(4001) that uvicorn answers with HTTP
    # 403. Reject-path closes never happen in bypass mode.
    close_codes = [c.kwargs.get("code") for c in ws.close.call_args_list]
    normalized = [1000 if c is None else c for c in close_codes]
    assert normalized == [1000] or normalized == [], f"unexpected closes: {close_codes}"
    assert 4001 not in close_codes and 4003 not in close_codes

    sent = [c.args[0] for c in ws.send_json.call_args_list if c.args]
    assert any(m.get("type") == "log" and "hello" in m.get("log", "") for m in sent)
    assert any(m.get("type") == "done" and m.get("status") == "completed" for m in sent)


@pytest.mark.asyncio
async def test_script_run_ws_session_mode_still_closes_without_cookie(monkeypatch):
    """AUTH_BYPASS off: no cookie -> pre-accept close(4001) is preserved."""
    from api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_bypass_enabled", False)
    monkeypatch.setattr("api.config.get_settings", lambda: settings)

    ws = _fake_ws()
    await script_run_ws("run-123", ws)

    ws.accept.assert_not_awaited()
    assert ws.close.call_args_list, "expected a pre-accept close"
    codes = [c.kwargs.get("code") for c in ws.close.call_args_list]
    assert 4001 in codes
