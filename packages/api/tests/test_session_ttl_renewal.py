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
