"""Async Redis client — session management and active-user tracking."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis

from api.config import get_settings

_pool: Redis | None = None


async def init_redis() -> None:
    global _pool
    settings = get_settings()
    _pool = aioredis.from_url(settings.effective_redis_url, decode_responses=True)


async def close_redis() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


def _get_pool() -> Redis:
    if _pool is None:
        raise RuntimeError("Redis not initialized — call init_redis() in app lifespan")
    return _pool


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency — yields the shared Redis connection pool."""
    yield _get_pool()


# ── Session ────────────────────────────────────────────────────────────────

async def save_session(redis: Redis, session_id: str, data: dict[str, Any], ttl: int) -> None:
    await redis.setex(f"session:{session_id}", ttl, json.dumps(data))


async def get_session(redis: Redis, session_id: str) -> dict[str, Any] | None:
    raw = await redis.get(f"session:{session_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # Corrupted/half-written session value (external tooling, Redis admin)
        # must read as "no session" (401), not crash the request with a 500.
        return None


async def renew_session_ttl(redis: Redis, session_id: str, ttl: int) -> None:
    """Extend the session TTL without re-writing session data.

    Uses EXPIRE on the existing key — cheaper than save_session and safe to
    call on every authenticated REST API request.  If the key has already
    expired the call is a no-op (EXPIRE on a missing key returns False).
    """
    await redis.expire(f"session:{session_id}", ttl)


async def delete_session(redis: Redis, session_id: str) -> None:
    await redis.delete(f"session:{session_id}")


# ── OAuth state (CSRF nonce) ───────────────────────────────────────────────

async def save_oauth_state(redis: Redis, state: str, ttl: int = 300) -> None:
    await redis.setex(f"oauth_state:{state}", ttl, "1")


async def validate_and_consume_state(redis: Redis, state: str) -> bool:
    """Returns True and deletes the nonce if it exists; False if it doesn't.

    Uses GETDEL for atomicity (Redis 6.2+). Falls back to a Lua script on
    older Redis to avoid a TOCTOU race where two concurrent callbacks could
    both see the key before either deletes it.
    """
    key = f"oauth_state:{state}"
    try:
        result = await redis.getdel(key)
    except Exception:
        _LUA = (
            "local v = redis.call('GET', KEYS[1]); "
            "if v then redis.call('DEL', KEYS[1]) end; "
            "return v"
        )
        result = await redis.eval(_LUA, 1, key)
    return bool(result)


# ── OAuth session cache (duplicate-callback resilience) ────────────────────

async def save_oauth_session(redis: Redis, state: str, session_id: str, ttl: int = 600) -> None:
    """Cache the session_id that resulted from an OAuth callback.

    Browsers (speculative prefetch, proxy retries) sometimes fire the
    callback URL twice: the first request consumes the OAuth state, and
    when the *real* navigation arrives the state is gone, but the session
    was already created by the first request.  This cache lets the
    duplicate callback recover the session_id and re-set the cookie
    instead of bouncing the user back to the login page.  TTL covers the
    full OAuth state lifetime (a prefetch can land minutes before the
    real navigation, e.g. while the user re-enters credentials after an
    SSO session timeout); the caller's anchor cookie is the replay gate.
    """
    await redis.setex(f"oauth_session:{state}", ttl, session_id)


async def get_oauth_session(redis: Redis, state: str) -> str | None:
    """Return the cached session_id for a state, if still valid."""
    return await redis.get(f"oauth_session:{state}")


async def delete_oauth_session(redis: Redis, state: str) -> None:
    await redis.delete(f"oauth_session:{state}")


# ── Active users ───────────────────────────────────────────────────────────

async def track_active_user(redis: Redis, user_id: str, window: int) -> None:
    """Record activity for user_id and evict members older than 2×window."""
    now = int(time.time())
    await redis.zadd("active_users", {user_id: now})
    await redis.zremrangebyscore("active_users", "-inf", now - window * 2)


async def get_active_users_count(redis: Redis, window: int) -> int:
    now = int(time.time())
    return await redis.zcount("active_users", now - window, "+inf")
