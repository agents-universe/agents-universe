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

    def __init__(self, service_key: str, detail: str = "", *, message: str | None = None):
        self.service_key = service_key
        if message is not None:
            # The token exists but cannot be used as-is (e.g. a pasted
            # trailing newline in a header value) — the default
            # "Token not configured" prefix would tell the user the
            # opposite, so the caller supplies the full message.
            super().__init__(message)
            return
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
                # NULL-ness explicitly — CASE, not a bare predicate: T-SQL
                # rejects ORDER BY on a boolean expression.
                "ORDER BY CASE WHEN environment IS NULL THEN 1 ELSE 0 END ASC, "
                "environment DESC"
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


# ---------------------------------------------------------------------------
# Email server integration (Settings → Integrations → 邮件服务器)
# ---------------------------------------------------------------------------

#: The one hint every "not configured" path surfaces to the user. Agents
#: holding the email-reader skill are instructed to relay it verbatim —
#: never to ask for a password in chat.
EMAIL_NOT_CONFIGURED_HINT = (
    "尚未配置邮件服务器。请在 Settings → 外部集成 → 邮件服务器 中添加："
    "填入 IMAP/SMTP 主机、端口、TLS 方式与用户名密码，可配置多台。"
)

_EMAIL_COLUMNS = (
    "email_server_id, slug, display_name, imap_host, imap_port, imap_security, "
    "smtp_host, smtp_port, smtp_security, username, key_hint, "
    "allow_private_network, enabled"
)


def _row_to_server(row) -> dict:
    """Map a raw email_servers row to a plain dict (no password column)."""
    return {
        "email_server_id": row[0],
        "slug": row[1],
        "display_name": row[2],
        "imap_host": row[3],
        "imap_port": int(row[4]),
        "imap_security": row[5],
        "smtp_host": row[6],
        "smtp_port": int(row[7]),
        "smtp_security": row[8],
        "username": row[9],
        "key_hint": row[10],
        # SQLite returns 0/1 for BIT/BOOLEAN columns — normalize to bool so
        # callers never see truthy ints on one dialect and True on another.
        "allow_private_network": bool(row[11]),
        "enabled": bool(row[12]),
    }


async def list_email_servers(context: ToolContext) -> list[dict]:
    """Enabled email servers of the current user — metadata only, no password.

    Missing table/db degrades to an empty list: the email tool turns that
    into EMAIL_NOT_CONFIGURED_HINT rather than crashing the conversation.
    """
    if not context.db_session:
        return []
    try:
        result = await context.db_session.execute(
            text(
                f"SELECT {_EMAIL_COLUMNS} FROM email_servers "
                "WHERE user_id = :uid AND enabled = 1 ORDER BY slug"
            ),
            {"uid": context.user_id},
        )
    except Exception as e:  # table absent on a pre-migration DB
        _log.warning("list_email_servers failed: %s", e)
        return []
    return [_row_to_server(row) for row in result.fetchall()]


async def get_email_server(context: ToolContext, slug: str) -> dict:
    """Resolve one configured server including its decrypted password.

    The password is for the email tool's internal use only — it must never
    appear in a tool result, log, or error message. Raises ToolAuthError
    with the user-facing configuration hint when the server is unknown,
    disabled, or the DB/session is unavailable.
    """
    not_configured = ToolAuthError(
        "email",
        message=f"邮件服务器 '{slug}' 未配置。{EMAIL_NOT_CONFIGURED_HINT}",
    )
    if not context.db_session:
        raise not_configured
    result = await context.db_session.execute(
        text(
            f"SELECT {_EMAIL_COLUMNS}, encrypted_password FROM email_servers "
            "WHERE user_id = :uid AND slug = :slug AND enabled = 1"
        ),
        {"uid": context.user_id, "slug": slug},
    )
    row = result.first()
    if not row:
        raise not_configured
    server = _row_to_server(row)
    try:
        server["password"] = _decrypt(row[13], context.user_id, context)
    except RuntimeError as e:
        raise ToolAuthError("email", message=f"邮件服务器 '{slug}' 密码解密失败：{e}")
    except Exception:
        raise ToolAuthError(
            "email",
            message=f"邮件服务器 '{slug}' 密码已损坏 — 请在 Settings → 外部集成 → 邮件服务器 中重新保存。",
        )
    return server
