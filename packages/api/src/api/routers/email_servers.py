"""Email server integration CRUD + live connection test.

User-scoped rows in ``email_servers`` (Settings → Integrations → 邮件服务器);
one row per (user_id, slug), so a user can register several mailboxes. The
password is stored AES-256-GCM encrypted (token_vault, user_id-derived key)
and never leaves the server: list responses carry only the last-4 key_hint,
and connection-test errors are scrubbed of the credential before returning.
"""
from __future__ import annotations

import asyncio
import logging
import re
import traceback

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.errors import api_error
from api.models.email_server import EmailServer
from api.services.token_vault import decrypt_or_none, encrypt, key_hint

router = APIRouter(prefix="/api/email/servers", tags=["email-servers"])

_log = logging.getLogger("agents_universe.email_servers")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SECURITY = ("ssl", "starttls", "none")


def _redact(text: str, *secrets: str) -> str:
    """Scrub submitted credentials out of an error message.

    IMAP/SMTP servers echo the LOGIN line in error responses ("LOGIN failed
    for user 'x' with password '...'"); returning that verbatim would print
    the password back to the client. Same rule as tokens.py / token_tests.py.
    """
    for secret in secrets:
        if not secret:
            continue
        text = text.replace(secret, "[REDACTED]")
        try:
            escaped = repr(secret.encode("utf-8"))[2:-1]
        except Exception:
            escaped = ""
        if escaped and escaped != secret:
            text = text.replace(escaped, "[REDACTED]")
    return text


class EmailServerBody(BaseModel):
    slug: str = Field(max_length=100)
    display_name: str | None = Field(None, max_length=255)
    imap_host: str = Field(max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_security: str = Field(default="ssl", max_length=10)
    smtp_host: str = Field(max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: str = Field(default="starttls", max_length=10)
    username: str = Field(max_length=255)
    # None on PUT => keep the stored password; required on POST.
    password: str | None = Field(None, max_length=2800)
    allow_private_network: bool = False
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]*$")
        return value

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def _strip_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host must not be empty")
        return value

    @field_validator("imap_security", "smtp_security")
    @classmethod
    def _check_security(cls, value: str) -> str:
        if value not in _SECURITY:
            raise ValueError(f"security must be one of {', '.join(_SECURITY)}")
        return value


def _to_public(row: EmailServer) -> dict:
    """Serialize a row for API responses — never includes the password."""
    return {
        "email_server_id": row.email_server_id,
        "slug": row.slug,
        "display_name": row.display_name,
        "imap_host": row.imap_host,
        "imap_port": row.imap_port,
        "imap_security": row.imap_security,
        "smtp_host": row.smtp_host,
        "smtp_port": row.smtp_port,
        "smtp_security": row.smtp_security,
        "username": row.username,
        "key_hint": row.key_hint,
        "allow_private_network": row.allow_private_network,
        "enabled": row.enabled,
    }


async def _get_owned(db: AsyncSession, user_id: str, server_id: str) -> EmailServer:
    result = await db.execute(
        select(EmailServer).where(
            EmailServer.email_server_id == server_id,
            EmailServer.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise api_error(404, "EMAIL_SERVER_NOT_FOUND", "Email server not found")
    return row


def _validate_outbound_host(host: str, port: int, allow_private: bool) -> None:
    """SSRF gate mirroring integrations.py base_url handling.

    Literal-IP / metadata-host checks run unconditionally; DNS resolution
    follows the SSRF_ENABLED opt-in. Internal mail servers opt out per row
    via allow_private_network.
    """
    if allow_private:
        return
    from agent_core.tools._ssrf import SSRFError, validate_url as ssrf_validate_url
    try:
        # https:// wrapper: validate_url only accepts http/https schemes, and
        # the host/metadata/IP checks it runs are scheme-independent.
        ssrf_validate_url(f"https://{host}:{port}", allow_any_port=True)
        from agent_core.tools._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from agent_core.tools._ssrf import resolve_and_validate
            resolve_and_validate(host, port)
    except SSRFError as e:
        raise api_error(
            400, "EMAIL_HOST_BLOCKED",
            f"Blocked mail host {host}: {e}. Enable 允许内网地址 for internal mail servers.",
        )


def _probe_imap(host: str, port: int, security: str, username: str, password: str) -> str:
    """Blocking IMAP LOGIN probe. Returns a short OK note; raises on failure."""
    import imaplib

    client = None
    try:
        if security == "ssl":
            client = imaplib.IMAP4_SSL(host, port, timeout=15)
        else:
            client = imaplib.IMAP4(host, port, timeout=15)
            if security == "starttls":
                client.starttls()
        typ, _ = client.login(username, password)
        return f"IMAP login ok ({typ})"
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def _probe_smtp(host: str, port: int, security: str, username: str, password: str) -> str:
    """Blocking SMTP AUTH probe. Returns a short OK note; raises on failure."""
    import smtplib

    client = None
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
            if security == "starttls":
                client.starttls()
        client.ehlo()
        client.login(username, password)
        return "SMTP auth ok"
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                pass


@router.get("")
async def list_email_servers(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(EmailServer)
        .where(EmailServer.user_id == current_user.user_id)
        .order_by(EmailServer.slug)
    )
    return [_to_public(row) for row in result.scalars().all()]


@router.post("")
async def create_email_server(
    body: EmailServerBody = ...,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if not body.password:
        raise api_error(400, "EMAIL_PASSWORD_REQUIRED", "password is required")
    user_id = current_user.user_id
    row = EmailServer(
        user_id=user_id,
        slug=body.slug,
        display_name=body.display_name or body.slug,
        imap_host=body.imap_host,
        imap_port=body.imap_port,
        imap_security=body.imap_security,
        smtp_host=body.smtp_host,
        smtp_port=body.smtp_port,
        smtp_security=body.smtp_security,
        username=body.username,
        encrypted_password=encrypt(body.password, user_id),
        key_hint=key_hint(body.password),
        allow_private_network=body.allow_private_network,
        enabled=body.enabled,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent POSTs with the same slug both pass the pre-insert
        # uniqueness window; the loser violates uq_email_server_slug.
        await db.rollback()
        raise api_error(409, "EMAIL_SERVER_EXISTS", f"Email server slug already exists: {body.slug}")
    await db.refresh(row)
    return _to_public(row)


@router.put("/{email_server_id}")
async def update_email_server(
    body: EmailServerBody = ...,
    email_server_id: str = Path(max_length=36),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    row = await _get_owned(db, current_user.user_id, email_server_id)
    user_id = current_user.user_id

    if body.slug != row.slug:
        dup = await db.execute(
            select(EmailServer).where(
                EmailServer.user_id == user_id,
                EmailServer.slug == body.slug,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise api_error(409, "EMAIL_SERVER_EXISTS", f"Email server slug already exists: {body.slug}")

    row.slug = body.slug
    row.display_name = body.display_name or body.slug
    row.imap_host = body.imap_host
    row.imap_port = body.imap_port
    row.imap_security = body.imap_security
    row.smtp_host = body.smtp_host
    row.smtp_port = body.smtp_port
    row.smtp_security = body.smtp_security
    row.username = body.username
    row.allow_private_network = body.allow_private_network
    row.enabled = body.enabled
    # None => keep the stored password (UI sends the field empty).
    if body.password:
        row.encrypted_password = encrypt(body.password, user_id)
        row.key_hint = key_hint(body.password)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise api_error(409, "EMAIL_SERVER_EXISTS", f"Email server slug already exists: {body.slug}")
    await db.refresh(row)
    return _to_public(row)


@router.delete("/{email_server_id}")
async def delete_email_server(
    email_server_id: str = Path(max_length=36),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    row = await _get_owned(db, current_user.user_id, email_server_id)
    await db.delete(row)
    await db.commit()
    return {"deleted": email_server_id}


@router.post("/{email_server_id}/test")
async def test_email_server(
    email_server_id: str = Path(max_length=36),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    row = await _get_owned(db, current_user.user_id, email_server_id)
    plain = decrypt_or_none(row.encrypted_password, current_user.user_id)
    if plain is None:
        raise api_error(400, "EMAIL_PASSWORD_CORRUPTED", "Stored password is corrupted — delete and re-save it")

    try:
        _validate_outbound_host(row.imap_host, row.imap_port, row.allow_private_network)
        _validate_outbound_host(row.smtp_host, row.smtp_port, row.allow_private_network)
    except Exception as exc:
        # api_error from the SSRF gate carries a safe message; re-raise it.
        if getattr(exc, "status_code", None) == 400:
            raise
        raise api_error(400, "EMAIL_HOST_BLOCKED", _redact(str(exc), plain))

    username, password = row.username, plain
    try:
        imap_note = await asyncio.to_thread(
            _probe_imap, row.imap_host, row.imap_port, row.imap_security, username, password
        )
    except Exception as exc:
        error = _redact(str(exc) or "IMAP connection failed", password, username)
        _log.warning(
            "email server %s: IMAP test failed: %s",
            row.slug, _redact(traceback.format_exc(), password, username),
        )
        return {"ok": False, "slug": row.slug, "imap": {"ok": False, "error": error}}

    try:
        smtp_note = await asyncio.to_thread(
            _probe_smtp, row.smtp_host, row.smtp_port, row.smtp_security, username, password
        )
    except Exception as exc:
        error = _redact(str(exc) or "SMTP connection failed", password, username)
        _log.warning(
            "email server %s: SMTP test failed: %s",
            row.slug, _redact(traceback.format_exc(), password, username),
        )
        return {
            "ok": False, "slug": row.slug,
            "imap": {"ok": True, "note": imap_note},
            "smtp": {"ok": False, "error": error},
        }

    return {
        "ok": True, "slug": row.slug,
        "imap": {"ok": True, "note": imap_note},
        "smtp": {"ok": True, "note": smtp_note},
    }
