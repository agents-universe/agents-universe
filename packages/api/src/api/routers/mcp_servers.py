"""MCP server registry — global tier CRUD + connection test.

Global servers (``project_id`` NULL) are cross-project, shared across users,
and managed here; their secrets resolve from ``user_tokens`` at runtime.
Project-scoped servers are synced from the project's
``knowledge/integrations/mcp-servers.md`` (see ``api/services/mcp_sync``) and
listed via ``GET /api/projects/{id}/mcp-servers`` — never managed here.

(project_id, slug) uniqueness is application-level (NULL-unique semantics
differ per dialect): two rows whose sanitized slugs collide are rejected.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.errors import api_error
from api.models.mcp_server import MCPServer
from api.models.user import UserToken

from agent_core.tools._mcp_catalog import mcp_row_to_config, sanitize_slug
from agent_core.tools.api_request import _is_sensitive_header

router = APIRouter(prefix="/api/mcp/servers", tags=["mcp-servers"])

_log = logging.getLogger("agents_universe.mcp_servers")

_TRANSPORTS = ("auto", "streamable_http", "sse")  # stdio reserved for v2
_AUTH_TYPES = ("none", "bearer", "header")


class MCPServerBody(BaseModel):
    # Widths mirror the model columns: an overlong value would otherwise fail
    # at commit (MSSQL) instead of being rejected at validation.
    slug: str = Field(max_length=100)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    transport: str = Field(default="auto", max_length=32)
    url: str | None = Field(default=None, max_length=1000)
    headers: dict[str, str] | None = None
    auth_type: str = Field(default="none", max_length=16)
    secret_ref: str | None = Field(default=None, max_length=100)
    secret_scope: str = Field(default="project", max_length=16)
    auth_header_name: str | None = Field(default=None, max_length=100)
    auth_value_template: str | None = Field(default=None, max_length=200)
    options: dict[str, Any] | None = None
    enabled: bool = True


def validate_mcp_server_body(body: MCPServerBody) -> None:
    """Raise a structured api_error on invalid input."""
    slug = body.slug.strip()
    if not slug or not sanitize_slug(slug):
        raise api_error(400, "MCP_SLUG_INVALID", "slug is required and must be a valid identifier")
    if body.transport not in _TRANSPORTS:
        raise api_error(400, "MCP_TRANSPORT_UNSUPPORTED", f"transport only supports {', '.join(_TRANSPORTS)} (stdio is reserved for v2)")
    # v1 transports all need a URL; the column is nullable only for the
    # reserved stdio path.
    if not body.url:
        raise api_error(400, "MCP_URL_REQUIRED", "url is required (v1 only supports HTTP transport)")
    if not body.url.lower().startswith(("http://", "https://")):
        raise api_error(400, "MCP_URL_INVALID", "url must be an http(s) address")
    if body.auth_type not in _AUTH_TYPES:
        raise api_error(400, "MCP_AUTH_TYPE_UNSUPPORTED", f"auth_type only supports {', '.join(_AUTH_TYPES)}")
    if body.auth_type != "none" and not body.secret_ref:
        raise api_error(400, "MCP_SECRET_REF_REQUIRED", "auth_type other than none requires secret_ref")
    if body.secret_scope not in ("project", "user"):
        raise api_error(400, "MCP_SECRET_SCOPE_INVALID", "secret_scope only supports project / user")
    # Sensitive header names (Authorization, X-API-Key, ...) must go through
    # auth.secret_ref — the runtime rejects them too, reject here at write.
    for name in (body.headers or {}):
        if _is_sensitive_header(name):
            raise api_error(400, "MCP_HEADER_SENSITIVE", f"header {name!r} is a sensitive field, configure auth via auth_type/secret_ref")
    # headers/options are serialized into String(4000) columns — reject an
    # oversize object at write time rather than at commit.
    for field_name, value in (("headers", body.headers), ("options", body.options)):
        if value:
            dumped = json.dumps(value, ensure_ascii=False)
            if len(dumped) > 4000:
                raise api_error(400, "MCP_FIELD_TOO_LARGE", f"{field_name} serializes to {len(dumped)} characters, max 4000")


def _apply_body(row: MCPServer, body: MCPServerBody) -> None:
    """Copy the request body onto the row.

    Only fields the client actually sent are written: the settings UI edits a
    subset of the schema (no ``headers``/``options``/``auth_value_template``),
    so unconditional assignment silently wiped the tool allowlist/denylist,
    custom headers and the auth header name on every edit. On create the
    skipped fields fall back to the column defaults, which match the schema
    defaults anyway.
    """
    provided = body.model_fields_set
    row.slug = body.slug.strip()
    # name is optional in the schema: only overwrite a stored display name
    # when the client actually sent it — a partial PUT that omits name must
    # not silently reset it to the slug.
    if "name" in provided:
        row.name = body.name or row.slug
    if row.name is None:
        row.name = row.slug
    if "description" in provided:
        row.description = body.description
    if "transport" in provided:
        row.transport = body.transport
    if "url" in provided:
        row.url = body.url
    if "headers" in provided:
        row.headers = _json_dump(body.headers)
    if "auth_type" in provided:
        row.auth_type = body.auth_type
    if "secret_ref" in provided:
        row.secret_ref = body.secret_ref
    if "secret_scope" in provided:
        row.secret_scope = body.secret_scope
    if "auth_header_name" in provided:
        row.auth_header_name = body.auth_header_name
    if "auth_value_template" in provided:
        row.auth_value_template = body.auth_value_template
    if "options" in provided:
        row.options = _json_dump(body.options)
    if "enabled" in provided:
        row.enabled = body.enabled


def _json_dump(v) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v else None


async def _assert_global_slug_unique(
    db: AsyncSession, slug: str, exclude_id: str | None = None,
) -> None:
    """Reject a slug that collides with an existing global row.

    Compared on the sanitized runtime key — "My Server" and "my_server" are
    the same server to the runtime, so they must not coexist.
    """
    key = sanitize_slug(slug)
    result = await db.execute(
        select(MCPServer).where(MCPServer.project_id.is_(None))
    )
    for row in result.scalars().all():
        if row.server_id == exclude_id:
            continue
        if sanitize_slug(row.slug) == key:
            raise api_error(409, "MCP_SERVER_SLUG_EXISTS", f"MCP server slug already exists: {row.slug}")


async def _configured_refs(
    db: AsyncSession, user_id: str, project_id: str | None, refs: set[str],
) -> set[str]:
    """Return which of ``refs`` have a stored secret for the given scope.

    Global servers (project_id None) resolve from user_tokens only; project
    servers resolve from project_secrets with a user_tokens fallback.
    """
    configured: set[str] = set()
    if not refs:
        return configured
    if project_id is not None:
        from api.models.project_secret import ProjectSecret
        ps_result = await db.execute(
            select(ProjectSecret.service_key).where(
                ProjectSecret.project_id == project_id,
                ProjectSecret.service_key.in_(refs),
                ProjectSecret.is_active == True,  # noqa: E712
            )
        )
        configured.update(row[0] for row in ps_result.fetchall())
        remaining = refs - configured
        if not remaining:
            return configured
        refs = remaining
    ut_result = await db.execute(
        select(UserToken.service_key).where(
            UserToken.user_id == user_id,
            UserToken.service_key.in_(refs),
        )
    )
    configured.update(row[0] for row in ut_result.fetchall())
    return configured


def serialize_mcp_server(
    row: MCPServer, has_secret: bool, scope: str,
) -> dict:
    """Project list endpoint and the CRUD list share one response shape.

    Metadata only — headers/options carry no secrets (validated at write),
    and the secret itself never leaves the server.
    """
    options = row.options_dict
    tools = options.get("tools") if isinstance(options.get("tools"), dict) else {}
    return {
        "server_id": row.server_id,
        "slug": row.slug,
        "name": row.name,
        "description": row.description,
        "url": row.url,
        "transport": row.transport,
        "enabled": bool(row.enabled),
        "auth_type": row.auth_type,
        "secret_ref": row.secret_ref,
        # Header name only (never the value) — the edit form needs it to
        # round-trip, otherwise saving the form clears it.
        "auth_header_name": row.auth_header_name,
        "has_secret": has_secret,
        "tool_allowlist": tools.get("allowlist") or [],
        "tool_denylist": tools.get("denylist") or [],
        "options": options,
        "scope": scope,
    }


async def _get_global_server(db: AsyncSession, server_id: str) -> MCPServer:
    result = await db.execute(
        select(MCPServer).where(
            MCPServer.server_id == server_id,
            MCPServer.project_id.is_(None),
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise api_error(404, "MCP_SERVER_NOT_FOUND", "MCP server not found")
    return row


# ── CRUD ──────────────────────────────────────────────────────────────────


@router.get("")
async def list_global_servers(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """List global MCP servers (metadata only, never secrets)."""
    result = await db.execute(
        select(MCPServer).where(MCPServer.project_id.is_(None)).order_by(MCPServer.name)
    )
    rows = result.scalars().all()
    refs = {r.secret_ref for r in rows if r.secret_ref}
    configured = await _configured_refs(db, current_user.user_id, None, refs)
    return [
        serialize_mcp_server(r, r.secret_ref in configured, "global")
        for r in rows
    ]


@router.post("", status_code=201)
async def create_global_server(
    body: MCPServerBody,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    validate_mcp_server_body(body)
    await _assert_global_slug_unique(db, body.slug)
    row = MCPServer(project_id=None, created_by=current_user.user_id)
    _apply_body(row, body)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # A fresh server has no stored secret yet — the UI prompts to save one.
    return serialize_mcp_server(row, False, "global")


@router.put("/{server_id}")
async def update_global_server(
    server_id: str,
    body: MCPServerBody,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    validate_mcp_server_body(body)
    row = await _get_global_server(db, server_id)
    await _assert_global_slug_unique(db, body.slug, exclude_id=server_id)
    _apply_body(row, body)
    await db.commit()
    await db.refresh(row)
    configured = await _configured_refs(db, _current_user.user_id, None, {row.secret_ref} if row.secret_ref else set())
    return serialize_mcp_server(row, row.secret_ref in configured, "global")


@router.delete("/{server_id}", status_code=204)
async def delete_global_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    _current_user: UserInfo = Depends(get_current_user),
):
    row = await _get_global_server(db, server_id)
    await db.delete(row)
    await db.commit()


# ── Connection test ───────────────────────────────────────────────────────


@router.post("/{server_id}/test")
async def test_global_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Connect to the server and discover its tools via MCPConnectionManager.

    Reuses the exact runtime path (header/secret resolution, SSRF/private-IP
    validation, per-server failures degrade to warnings).  Returns how many
    tools were discovered; never leaks credentials or raw exception chains.
    """
    row = await _get_global_server(db, server_id)
    cfg = mcp_row_to_config(row)
    slug = sanitize_slug(row.slug)

    from agent_core.tools.base import ToolContext
    from agent_core.tools.mcp_client import McpConnectionManager

    settings = get_settings()
    ctx = ToolContext(
        project_id="mcp-test",
        project_fs_path="",
        conversation_id="",
        user_id=current_user.user_id,
        db_session=db,
        secret_key=settings.secret_key,
    )
    ctx.ssl_verify = settings.llm_ssl_verify
    manager = McpConnectionManager(ctx)
    try:
        tools = await manager.discover_tools({slug: cfg}, {slug})
    except Exception as exc:
        _log.warning("MCP server %r test connection failed: %s", row.slug, exc)
        return {"ok": False, "tools": 0, "error": str(exc)[:300]}
    finally:
        await manager.close_all()
    if not tools:
        return {
            "ok": False,
            "tools": 0,
            "error": "连接失败或未发现工具（已检查 URL 安全、认证头与连接超时）",
        }
    return {"ok": True, "tools": len(tools)}
