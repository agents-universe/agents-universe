"""Token retrieval and decryption for agent tools.

Standalone implementation — does NOT import from the api package.
Uses the same AES-256-GCM scheme as api/services/token_vault.py.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from .base import ToolContext

_log = logging.getLogger(__name__)

_ITERATIONS = 100_000
_KEY_LEN = 32


class ToolAuthError(Exception):
    """Raised when a required token is missing or decryption fails."""

    def __init__(self, service_key: str, detail: str = ""):
        self.service_key = service_key
        msg = f"Token not configured: '{service_key}'. Ask the user to add it in Settings → Integrations."
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


def _get_secret_key(context: "ToolContext | None" = None) -> str:
    # Prefer the value injected into ToolContext (set by API from pydantic settings)
    # so that pydantic's .env loading is honoured even when os.environ is not set.
    if context is not None and getattr(context, "secret_key", ""):
        return context.secret_key
    key = os.environ.get("SECRET_KEY", "")
    if not key:
        raise RuntimeError("SECRET_KEY env var not set — cannot decrypt tokens")
    return key


def _derive_key(secret_key: str, user_id: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode(),
        user_id.encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )


def _decrypt(ciphertext: str, user_id: str, context: "ToolContext | None" = None) -> str:
    key = _derive_key(_get_secret_key(context), user_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


async def get_token(context: ToolContext, service_key: str) -> str:
    """Retrieve and decrypt a single token for the current user.

    Reads from user_tokens table via context.db_session.
    Raises ToolAuthError if not found or decryption fails.
    """
    if not context.db_session:
        raise ToolAuthError(service_key, "no database session available")

    result = await context.db_session.execute(
        text(
            "SELECT encrypted_value FROM user_tokens "
            "WHERE user_id = :uid AND service_key = :skey"
        ),
        {"uid": context.user_id, "skey": service_key},
    )
    row = result.first()
    if not row:
        raise ToolAuthError(service_key)

    try:
        return _decrypt(row[0], context.user_id, context)
    except RuntimeError as e:
        raise ToolAuthError(service_key, str(e))
    except Exception:
        raise ToolAuthError(service_key, "decryption failed — token may be corrupted")


async def get_tokens(context: ToolContext, *service_keys: str) -> dict[str, str]:
    """Retrieve and decrypt multiple tokens. Raises on first missing key."""
    tokens = {}
    for key in service_keys:
        tokens[key] = await get_token(context, key)
    return tokens


async def get_token_optional(context: ToolContext, service_key: str) -> str | None:
    """Like get_token but returns None instead of raising on missing."""
    try:
        return await get_token(context, service_key)
    except ToolAuthError:
        return None


# ---------------------------------------------------------------------------
# Project-scoped secret resolver
# Priority: project_secrets → user_tokens → ToolAuthError
# ---------------------------------------------------------------------------

def _derive_project_key(secret_key: str, project_id: str) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_key.encode(),
        f"project:{project_id}".encode(),
        _ITERATIONS,
        dklen=_KEY_LEN,
    )


def _decrypt_project_secret(ciphertext: str, project_id: str, context: "ToolContext | None" = None) -> str:
    key = _derive_project_key(_get_secret_key(context), project_id)
    aesgcm = AESGCM(key)
    blob = base64.b64decode(ciphertext)
    nonce = blob[:12]
    ct = blob[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


async def get_secret(context: ToolContext, service_key: str, *, environment: str | None = None) -> str:
    """Retrieve a secret: project-level first, then user-level fallback.

    Project secrets belong to the project and are accessible by any user
    with project access. User tokens are per-user fallback for legacy configs.
    """
    if not context.db_session:
        raise ToolAuthError(service_key, "no database session available")

    # 1. Try project_secrets — exact environment match first, then env-agnostic fallback
    # is_active is a bound bool (not = 1): SQL Server stores BIT, PostgreSQL
    # BOOLEAN — literal comparisons are dialect-specific.
    base_params: dict = {"pid": context.project_id, "skey": service_key, "active": True}

    candidates: list[tuple[str, dict]] = []
    if environment:
        candidates.append(("AND environment = :env", {**base_params, "env": environment}))
    # Always try env-agnostic as a fallback (covers tokens saved without an
    # env tag). Save paths normalize a missing environment to "" — SQL Server
    # unique indexes treat NULL as distinct, so new rows store '' — but
    # legacy rows hold NULL. Match both.
    candidates.append(("AND (environment IS NULL OR environment = '')", base_params))

    for env_clause, params in candidates:
        result = await context.db_session.execute(
            text(
                "SELECT encrypted_value FROM project_secrets "
                f"WHERE project_id = :pid AND service_key = :skey {env_clause} "
                "AND is_active = :active AND secret_name = 'default' "
                # Legacy duplicates can linger (NULL + "" rows for the same
                # key) — prefer the "" row. NULL sort order in DESC differs
                # per dialect (PostgreSQL puts NULL first), so order by the
                # NULL-ness predicate explicitly — portable everywhere.
                "ORDER BY (environment IS NULL) ASC, environment DESC"
            ),
            params,
        )
        row = result.first()
        if row:
            _log.warning(
                "get_secret: hit project_secrets key=%r env_clause=%r project_id=%r",
                service_key, env_clause, context.project_id,
            )
            try:
                return _decrypt_project_secret(row[0], context.project_id, context)
            except Exception:
                raise ToolAuthError(service_key, "project secret decryption failed")

    # 2. Fallback to user_tokens (legacy)
    _log.warning(
        "get_secret: project_secrets miss for ALL candidates — service_key=%r environment=%r project_id=%r; falling back to user_tokens",
        service_key, environment, context.project_id,
    )
    token = await get_token(context, service_key)
    _log.warning(
        "get_secret: hit user_tokens key=%r project_id=%r",
        service_key, context.project_id,
    )
    return token


async def get_secret_optional(context: ToolContext, service_key: str, *, environment: str | None = None) -> str | None:
    """Like get_secret but returns None instead of raising on missing."""
    try:
        return await get_secret(context, service_key, environment=environment)
    except ToolAuthError:
        return None
