"""OAuth 2.0 Authorization Code flow via OIDC discovery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.services.oidc_discovery import discover
from api.services.preferences import get_or_create_preferences, serialize_preferences
from api.services.redis_client import (
    delete_session,
    get_active_users_count,
    get_oauth_session,
    get_redis,
    get_session,
    save_oauth_session,
    save_oauth_state,
    save_session,
    validate_and_consume_state,
)

router = APIRouter(tags=["auth"])

_log = logging.getLogger("agents_universe.auth")

# Signed anchor cookie bound to an OAuth state. The duplicate-callback cache
# is keyed by state alone, which made the state a replayable credential
# : anyone who saw the callback URL could replay it within the
# cache window and receive the victim's session cookie. Replays must now
# also present this cookie, which only the browser that completed the
# original login holds. Browser prefetch/speculative navigation still works
# (top-level navigation responses set cookies); if a browser doesn't, the
# flow degrades to the pre-cache behavior (redirect to login), never to
# session theft.
_ANCHOR_COOKIE = "oauth_anchor"
# Anchor and oauth_session cache TTLs must cover the full state lifetime
# (save_oauth_state in /auth/login): a speculative callback request can
# consume the state minutes before the real navigation arrives — e.g. while
# the user is still typing credentials after an SSO session timeout. All
# three are driven by settings.oauth_state_ttl so they expire together.


def _anchor_value(state: str) -> str:
    settings = get_settings()
    return hmac.new(settings.secret_key.encode(), state.encode(), hashlib.sha256).hexdigest()


def _check_logout_origin(request: Request, settings) -> None:
    """Reject cross-site logout requests (CSRF on a GET endpoint).

    Allowed referers: the web app origin, the SSO origin (the OAuth
    end-session redirect chain can land here from the IdP), the API's own
    host (the API serves the built frontend, and the user may reach it via
    any hostname/IP), and loopback names for dev servers. A cross-site page
    cannot place itself on the victim's loopback or forge its referer to the
    API's own host, so these additions do not weaken the CSRF gate.
    """
    from urllib.parse import urlsplit

    referer = request.headers.get("referer") or request.headers.get("origin")
    if not referer:
        return  # no referer: the user is acting directly (curl, API client)
    try:
        ref_origin = urlsplit(referer)
        ref_host = ref_origin.hostname or ""
    except ValueError:
        return
    allowed = set()
    for base in (settings.web_base_url, settings.oauth_sso_domain):
        try:
            parsed = urlsplit(base)
            if parsed.hostname:
                allowed.add(parsed.hostname.lower())
        except ValueError:
            continue
    # The API serves the frontend itself — the UI may be visited under any
    # hostname the API is reachable by, not just web_base_url.
    api_host = (request.headers.get("host") or "").strip()
    if api_host:
        try:
            _h = urlsplit(f"//{api_host}").hostname
            if _h:
                allowed.add(_h.lower())
        except ValueError:
            pass
    # Dev servers and direct local access (browser page on localhost).
    allowed.update({"localhost", "127.0.0.1", "::1"})
    if ref_host.lower() not in allowed:
        _log.warning("Cross-site logout attempt blocked: referer=%s", referer[:120])
        raise HTTPException(status_code=403, detail="Cross-site logout blocked")


def _basic_auth(client_id: str, client_secret: str) -> str:
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


@router.get("/auth/login")
async def login(redis: Redis = Depends(get_redis)):
    """Generate a CSRF-safe state nonce and redirect to the SSO authorize endpoint."""
    settings = get_settings()
    endpoints = await discover(settings.oauth_sso_domain)
    state = str(uuid.uuid4())
    await save_oauth_state(redis, state, ttl=settings.oauth_state_ttl)

    params = {
        "response_type": "code",
        "client_id": settings.oauth_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "scope": settings.oauth_scope,
        "state": state,
    }
    if settings.oauth_acr_values:
        params["acr_values"] = settings.oauth_acr_values

    auth_url = f"{endpoints.authorization_endpoint}?{urlencode(params)}"
    response = RedirectResponse(url=auth_url)
    # Anchor cookie is issued *before* the IdP round-trip, not only in the
    # callback. A speculative callback request (browser prefetch / proxy
    # retry) consumes the state, and the real navigation can only recover
    # the cached session if it can present this cookie. This same-origin
    # response reliably commits Set-Cookie, whereas a cross-site
    # speculative prefetch of the callback may have its cookies dropped —
    # which previously bounced the user back to the login page on the
    # first attempt (desktop Chrome; mobile usually doesn't prefetch).
    response.set_cookie(
        key=_ANCHOR_COOKIE,
        value=_anchor_value(state),
        max_age=settings.oauth_state_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.get("/auth/callback")
async def auth_callback(
    code: str,
    state: str,
    request: Request,
    redis: Redis = Depends(get_redis),
):
    """Exchange the authorization code for tokens, create a Redis session.

    Browsers frequently fire the callback URL twice (speculative
    prefetch, proxy retries), which consumes the OAuth state before the
    real navigation arrives.  To handle this we cache the resulting
    session_id keyed by state: a duplicate callback recovers the session
    and re-sets the cookie instead of bouncing the user to the login page.
    """
    settings = get_settings()
    endpoints = await discover(settings.oauth_sso_domain)

    if not await validate_and_consume_state(redis, state):
        # State was already consumed — likely a duplicate callback from
        # browser prefetch or a reverse-proxy retry.  Check whether a
        # session was already created for this state; if so, re-set the
        # cookie and redirect so the user lands inside the app.
        cached_session_id = await get_oauth_session(redis, state)
        if cached_session_id and request.cookies.get(_ANCHOR_COOKIE) == _anchor_value(state):
            _log.info(
                "Duplicate OAuth callback; re-setting cookie from cached session. state=%s",
                state[:8],
            )
            response = RedirectResponse(url=settings.web_base_url)
            response.set_cookie(
                key=settings.auth_cookie_name,
                value=cached_session_id,
                max_age=settings.session_ttl,
                httponly=True,
                samesite="lax",
                secure=settings.cookie_secure,
                path="/",
            )
            return response

        # Genuinely invalid — state expired or was never saved. Re-issue the
        # login redirect instead of dropping the user back to the web base URL
        # with no session: if the SSO session is still valid, Casdoor bounces
        # straight back through a fresh state and the user lands signed in
        # without seeing a second login page.
        _log.warning(
            "OAuth state not found (expired or invalid); re-issuing login. state=%s",
            state[:8],
        )
        return RedirectResponse(url="/auth/login")

    basic = _basic_auth(settings.oauth_client_id, settings.oauth_client_secret)

    async with httpx.AsyncClient(verify=settings.oauth_ssl_verify, timeout=10.0) as client:
        token_resp = await client.post(
            endpoints.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oauth_redirect_uri,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Token exchange failed")

        try:
            access_token = token_resp.json().get("access_token")
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid token response from SSO")
        if not access_token:
            raise HTTPException(status_code=401, detail="No access_token in SSO response")

        userinfo_resp = await client.get(
            endpoints.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Userinfo fetch failed")

    try:
        userinfo = userinfo_resp.json()
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid userinfo response from SSO")
    user_id = (userinfo.get("sub") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="No sub in userinfo response")

    session_id = str(uuid.uuid4())
    display_name = (
        userinfo.get("display_name")
        or userinfo.get("name")
        or user_id
    )
    await save_session(
        redis,
        session_id,
        {
            "user_id": user_id,
            "display_name": display_name,
            "access_token": access_token,
        },
        ttl=settings.session_ttl,
    )

    # Cache the session for a short window so that a duplicate callback
    # (browser prefetch / proxy retry) can recover it without re-exchanging
    # the one-time authorization code.
    await save_oauth_session(redis, state, session_id, ttl=settings.oauth_state_ttl)

    response = RedirectResponse(url=settings.web_base_url)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=session_id,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    # Anchor cookie : proves to the duplicate-callback path that
    # this browser went through this state's login flow (it is also set
    # in /auth/login, before the IdP round-trip). Not a credential
    # itself — it's only compared against the HMAC of the state.
    response.set_cookie(
        key=_ANCHOR_COOKIE,
        value=_anchor_value(state),
        max_age=settings.oauth_state_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.get("/auth/logout")
async def logout(request: Request, redis: Redis = Depends(get_redis)):
    """Revoke the SSO token, delete the Redis session, and clear the cookie.

    a third-party page can trigger a cross-site GET (<img
    src="/auth/logout">) to force-logout a victim — SameSite=Lax cookies are
    sent on image requests. Logout is initiated by a top-level navigation
    (no Origin header), so gate on the referer when present: requests with
    no referer (curl, API clients) are the user acting on their own session.
    """
    settings = get_settings()
    _check_logout_origin(request, settings)
    try:
        endpoints = await discover(settings.oauth_sso_domain)
    except Exception as e:
        # Discovery must never block logout — the local session still has to
        # be dropped even when the IdP is unreachable.
        _log.warning("SSO discovery failed during logout: %s", e)
        endpoints = None
    session_id = request.cookies.get(settings.auth_cookie_name)

    if session_id:
        data = await get_session(redis, session_id)
        if data:
            access_token = data.get("access_token")
            if access_token and endpoints and endpoints.revocation_endpoint:
                basic = _basic_auth(settings.oauth_client_id, settings.oauth_client_secret)
                try:
                    async with httpx.AsyncClient(verify=settings.oauth_ssl_verify, timeout=5.0) as client:
                        await client.post(
                            endpoints.revocation_endpoint,
                            data={"token": access_token, "token_type_hint": "access_token"},
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Authorization": f"Basic {basic}",
                            },
                        )
                except Exception as e:
                    _log.warning("SSO token revocation failed during logout: %s", e)
        await delete_session(redis, session_id)

    if endpoints and endpoints.end_session_endpoint:
        response = RedirectResponse(url=endpoints.end_session_endpoint)
    else:
        response = RedirectResponse(url=settings.web_base_url)
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    return response


@router.get("/api/me")
async def me(
    current_user: UserInfo = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = await get_or_create_preferences(db, current_user.user_id)
    return {
        "user_id": current_user.user_id,
        "display_name": current_user.display_name,
        "preferences": serialize_preferences(prefs),
    }


@router.get("/api/active-users")
async def active_users(
    redis: Redis = Depends(get_redis),
    current_user: UserInfo = Depends(get_current_user),
):
    settings = get_settings()
    count = await get_active_users_count(redis, settings.active_users_window)
    return {"count": count}
