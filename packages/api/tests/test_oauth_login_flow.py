"""OAuth SSO login-flow tests (state expiry self-heal + duplicate-callback recovery).

The auth router is exercised through the app with a fake in-memory Redis, so
these tests assert the exact HTTP responses a browser sees:

- An expired OAuth state must re-issue the login redirect (self-heal), not
  bounce the browser to the web base URL with no session cookie.
- A duplicate callback (state already consumed) must recover the cached
  session when the anchor cookie matches, re-setting the session cookie.
- The success path must set both the session cookie and the anchor cookie and
  persist the Redis session.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from urllib.parse import parse_qs, urlparse

from httpx import ASGITransport, AsyncClient
import pytest


class FakeRedis:
    """In-memory stand-in for the Redis API surface the auth flow touches."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def setex(self, key, ttl, value):
        # TTL is intentionally ignored: expiry is simulated by deleting keys.
        self._store[key] = value

    async def getdel(self, key):
        return self._store.pop(key, None)

    async def eval(self, _script, _numkeys, key):
        # Lua fallback path used when getdel raises.
        return self._store.pop(key, None)

    async def get(self, key):
        return self._store.get(key)


@pytest.fixture
def fake_redis(app_fixture):
    from api.services.redis_client import get_redis

    store = FakeRedis()

    async def _fake_redis():
        yield store

    # Preserve the session-scoped override installed by conftest's _no_redis
    # fixture — popping it here would strip every later test of its Redis
    # stand-in and fail them with "Redis not initialized".
    prev = app_fixture.dependency_overrides.get(get_redis)
    app_fixture.dependency_overrides[get_redis] = _fake_redis
    yield store
    if prev is None:
        app_fixture.dependency_overrides.pop(get_redis, None)
    else:
        app_fixture.dependency_overrides[get_redis] = prev


@pytest.fixture
def oauth_settings(app_fixture, monkeypatch):
    """Point discovery at a fake IdP and set the cookie/state knobs."""
    from api.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "oauth_sso_domain", "https://sso.test")
    monkeypatch.setattr(settings, "oauth_client_id", "cid")
    monkeypatch.setattr(settings, "oauth_client_secret", "secret")
    monkeypatch.setattr(settings, "oauth_redirect_uri", "http://test/auth/callback")
    monkeypatch.setattr(settings, "web_base_url", "http://test/app")
    # cookie_secure is a read-only property; drive it via its backing field.
    monkeypatch.setattr(settings, "oauth_secure_cookie", False)
    monkeypatch.setattr(settings, "auth_cookie_name", "x-auth-token")
    monkeypatch.setattr(settings, "oauth_state_ttl", 1800)
    return settings


@pytest.fixture
def idp(oauth_settings, monkeypatch):
    """Fake OIDC discovery endpoints — authorization always redirects back."""
    from api.services import oidc_discovery

    async def _discover(_issuer):
        return oidc_discovery.OIDCEndpoints(
            authorization_endpoint="https://sso.test/authorize",
            token_endpoint="https://sso.test/token",
            userinfo_endpoint="https://sso.test/userinfo",
        )

    monkeypatch.setattr("api.routers.auth.discover", _discover)
    return _discover


@pytest.fixture
async def client(app_fixture):
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _start_login(client):
    resp = await client.get("/auth/login")
    assert resp.status_code in (302, 307)
    location = urlparse(resp.headers["location"])
    params = parse_qs(location.query)
    return params["state"][0], resp


def _anchor_cookie(state: str, settings) -> str:
    return hmac.new(
        settings.secret_key.encode(), state.encode(), hashlib.sha256
    ).hexdigest()


# ── expired state self-heals back to /auth/login ───────────────────────────

async def test_expired_state_redirects_to_login(client, fake_redis, idp):
    """A callback whose state is gone re-issues login instead of bouncing home."""
    state = uuid.uuid4().hex
    # No state saved in fake_redis — simulates expiry/restart.
    resp = await client.get(
        "/auth/callback", params={"code": "c", "state": state}
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/auth/login"
    # No session cookie must be set on the failed callback.
    assert "x-auth-token=" not in (resp.headers.get("set-cookie") or "")


# ── duplicate callback recovers the cached session via the anchor cookie ───

async def test_duplicate_callback_recovers_session(
    client, fake_redis, idp, oauth_settings
):
    """State already consumed + matching anchor → session cookie re-issued."""
    state = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    # First callback already created a session; cache it keyed by state.
    fake_redis._store[f"oauth_session:{state}"] = session_id

    resp = await client.get(
        "/auth/callback",
        params={"code": "c", "state": state},
        cookies={"oauth_anchor": _anchor_cookie(state, oauth_settings)},
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "http://test/app"
    assert f"x-auth-token={session_id}" in (resp.headers.get("set-cookie") or "")


async def test_duplicate_callback_without_anchor_self_heals(
    client, fake_redis, idp, oauth_settings
):
    """Consumed state but no anchor cookie → re-issue login, no session leak."""
    state = uuid.uuid4().hex
    fake_redis._store[f"oauth_session:{state}"] = uuid.uuid4().hex

    resp = await client.get(
        "/auth/callback", params={"code": "c", "state": state}
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/auth/login"
    assert "x-auth-token=" not in (resp.headers.get("set-cookie") or "")


# ── success path sets cookies and persists the session ─────────────────────

async def test_success_sets_cookies_and_session(
    client, fake_redis, idp, oauth_settings, monkeypatch
):
    """Full success path: token exchange → session persisted → both cookies set."""
    import httpx as _httpx

    from api.services.oidc_discovery import OIDCEndpoints

    state = uuid.uuid4().hex
    fake_redis._store[f"oauth_state:{state}"] = "1"

    async def _fake_token_post(url, **kwargs):
        assert kwargs["data"]["grant_type"] == "authorization_code"
        return _httpx.Response(200, json={"access_token": "at-1"})

    async def _fake_userinfo_get(url, **kwargs):
        return _httpx.Response(
            200, json={"sub": "u-1", "name": "Alice"}
        )

    monkeypatch.setattr(
        "api.routers.auth.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(_fake_token_post, _fake_userinfo_get),
    )

    resp = await client.get(
        "/auth/callback", params={"code": "c", "state": state}
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "http://test/app"

    set_cookie = resp.headers.get("set-cookie") or ""
    assert "x-auth-token=" in set_cookie
    assert "oauth_anchor=" in set_cookie

    # Session persisted under session:{id} with the user info.
    session_keys = [k for k in fake_redis._store if k.startswith("session:")]
    assert len(session_keys) == 1
    import json

    data = json.loads(fake_redis._store[session_keys[0]])
    assert data["user_id"] == "u-1"
    assert data["display_name"] == "Alice"


class _FakeAsyncClient:
    """Minimal async context manager standing in for httpx.AsyncClient."""

    def __init__(self, post, get):
        self._post = post
        self._get = get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        return await self._post(url, **kwargs)

    async def get(self, url, **kwargs):
        return await self._get(url, **kwargs)
