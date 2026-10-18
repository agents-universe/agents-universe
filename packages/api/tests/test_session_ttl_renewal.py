"""Unit tests for session TTL renewal on REST API requests.

Verifies that get_current_user renews the Redis session TTL on every
authenticated request, fixing the bug where sessions expired after 24 h
of REST-only activity (e.g. loading conversation history) with no
WebSocket messages to refresh the TTL.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response


@pytest.fixture
def _mock_settings(mocker):
    """Return a MagicMock settings with auth bypass disabled."""
    s = MagicMock()
    s.auth_bypass_enabled = False
    s.auth_cookie_name = "x-auth-token"
    s.session_ttl = 86400
    s.active_users_window = 300
    s.cookie_secure = False
    mocker.patch("api.dependencies.auth.get_settings", return_value=s)
    return s


@pytest.fixture
def _mock_redis():
    """An AsyncMock standing in for the Redis connection."""
    return AsyncMock()


def _make_request(cookie_value: str | None = "valid-session-id"):
    req = MagicMock()
    if cookie_value:
        req.cookies = {"x-auth-token": cookie_value}
    else:
        req.cookies = {}
    return req


class _FakeWS:
    """One message frame, then a disconnect."""

    def __init__(self, message: dict) -> None:
        self.cookies = {"x-auth-token": "sid-1"}
        self.sent: list[dict] = []
        self._message = json.dumps(message)

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if self._message is not None:
            message, self._message = self._message, None
            return message
        from starlette.websockets import WebSocketDisconnect
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000) -> None:
        pass


class _FakeRedis:
    def __init__(self) -> None:
        self.expire_calls: list[tuple[str, int]] = []
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key: str) -> str:
        return json.dumps({"user_id": "test-user", "display_name": "Test"})

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.setex_calls.append((key, ttl, value))


async def test_ws_message_renews_the_session_without_recreating_it(
    db, make_project, monkeypatch
):
    """A socket opened before logout must not resurrect the deleted session.

    The handler used to re-write the cached session payload with SETEX on
    every message, so a session revoked elsewhere came back to life. Renewal
    is an EXPIRE on the existing key: missing stays missing.
    """
    from types import SimpleNamespace

    from api.models.conversation import Conversation
    from api.websocket.handlers import conversation_ws
    from api.websocket.manager import ConnectionManager

    project = await make_project()
    conv = Conversation(
        project_id=project.project_id,
        user_id="test-user",
        status="active",
        title="renew",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    fake_redis = _FakeRedis()
    monkeypatch.setattr(
        "api.services.redis_client._get_pool", lambda: fake_redis
    )
    monkeypatch.setattr(
        "api.config.get_settings",
        lambda: SimpleNamespace(
            auth_bypass_enabled=False,
            auth_bypass_user_id=None,
            auth_cookie_name="x-auth-token",
            session_ttl=3600,
        ),
    )
    monkeypatch.setattr(
        "api.websocket.handlers.manager", ConnectionManager()
    )

    async def _noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("api.websocket.handlers._handle_message", _noop)

    ws = _FakeWS({"type": "message", "content": "hi"})
    await conversation_ws(conv.conversation_id, ws)

    assert fake_redis.expire_calls == [("session:sid-1", 3600)]
    assert fake_redis.setex_calls == []


# ── get_current_user renews TTL after valid session ──────────────

async def test_renews_ttl_after_valid_session(_mock_settings, _mock_redis):
    """A successful session read must be followed by redis.expire and a refreshed cookie."""
    from api.dependencies.auth import get_current_user

    _mock_redis.get = AsyncMock(
        return_value=json.dumps({"user_id": "u-1", "display_name": "Alice"})
    )

    response = Response()
    user = await get_current_user(
        request=_make_request(), response=response, redis=_mock_redis
    )

    assert user.user_id == "u-1"
    assert user.display_name == "Alice"
    _mock_redis.expire.assert_awaited_once_with(
        "session:valid-session-id", 86400
    )
    # Sliding cookie: the browser cookie is refreshed with the session TTL.
    assert response.headers.get("set-cookie")
    assert "x-auth-token=valid-session-id" in response.headers["set-cookie"]


# ── TTL is NOT renewed when session is missing ───────────────────

async def test_does_not_renew_when_session_missing(_mock_settings, _mock_redis):
    """If the session key is gone, get_current_user must 401 without calling expire."""
    from api.dependencies.auth import get_current_user

    _mock_redis.get = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            request=_make_request("ghost-id"), response=Response(), redis=_mock_redis
        )

    assert exc.value.status_code == 401
    _mock_redis.expire.assert_not_awaited()


# ── TTL is NOT renewed when session data lacks user_id ───────────

async def test_does_not_renew_when_session_data_corrupt(_mock_settings, _mock_redis):
    """Session JSON without user_id is treated as invalid — no TTL renewal."""
    from api.dependencies.auth import get_current_user

    _mock_redis.get = AsyncMock(return_value=json.dumps({"foo": "bar"}))

    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            request=_make_request(), response=Response(), redis=_mock_redis
        )

    assert exc.value.status_code == 401
    _mock_redis.expire.assert_not_awaited()


# ── TTL is NOT renewed when no cookie is present ─────────────────

async def test_does_not_renew_when_no_cookie(_mock_settings, _mock_redis):
    """Missing cookie -> 401 before any Redis call."""
    from api.dependencies.auth import get_current_user

    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            request=_make_request(None), response=Response(), redis=_mock_redis
        )

    assert exc.value.status_code == 401
    _mock_redis.expire.assert_not_awaited()
    _mock_redis.get.assert_not_awaited()


# ── TTL is NOT renewed in auth bypass mode ───────────────────────

async def test_does_not_renew_in_bypass_mode(mocker, _mock_redis):
    """Auth bypass short-circuits before any Redis interaction."""
    from api.dependencies.auth import get_current_user

    s = MagicMock()
    s.auth_bypass_enabled = True
    s.auth_bypass_user_id = "bypass-user"
    mocker.patch("api.dependencies.auth.get_settings", return_value=s)

    user = await get_current_user(
        request=_make_request(None), response=Response(), redis=_mock_redis
    )

    assert user.user_id == "bypass-user"
    _mock_redis.expire.assert_not_awaited()
    _mock_redis.get.assert_not_awaited()


# ── renew_session_ttl uses redis.expire (not setex) ──────────────

async def test_renew_session_ttl_calls_expire():
    """renew_session_ttl should call redis.expire, not re-write session data."""
    from api.services.redis_client import renew_session_ttl

    redis = AsyncMock()
    await renew_session_ttl(redis, "abc-123", 3600)

    redis.expire.assert_awaited_once_with("session:abc-123", 3600)
    # Must NOT use setex (which would overwrite session data unnecessarily)
    redis.setex.assert_not_awaited()


# ── TTL value matches settings.session_ttl ───────────────────────

async def test_ttl_matches_settings(_mock_redis, mocker):
    """The TTL passed to expire must equal settings.session_ttl."""
    from api.dependencies.auth import get_current_user

    s = MagicMock()
    s.auth_bypass_enabled = False
    s.auth_cookie_name = "x-auth-token"
    s.session_ttl = 7200  # non-default to catch hard-coding
    s.active_users_window = 300
    s.cookie_secure = False
    mocker.patch("api.dependencies.auth.get_settings", return_value=s)

    _mock_redis.get = AsyncMock(
        return_value=json.dumps({"user_id": "u-2", "display_name": "Bob"})
    )

    response = Response()
    await get_current_user(request=_make_request(), response=response, redis=_mock_redis)

    _mock_redis.expire.assert_awaited_once_with("session:valid-session-id", 7200)
    # Sliding cookie max_age follows the (non-default) session_ttl.
    assert "Max-Age=7200" in response.headers["set-cookie"]
