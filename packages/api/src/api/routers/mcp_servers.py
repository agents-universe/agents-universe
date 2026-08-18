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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.mcp_server import MCPServer
from api.models.user import UserToken

from agent_core.tools._mcp_catalog import mcp_row_to_config, sanitize_slug
from agent_core.tools.api_request import _is_sensitive_header

router = APIRouter(prefix="/api/mcp/servers", tags=["mcp-servers"])

_log = logging.getLogger("agents_universe.mcp_servers")

_TRANSPORTS = ("auto", "streamable_http", "sse")  # stdio reserved for v2
_AUTH_TYPES = ("none", "bearer", "header")


class MCPServerBody(BaseModel):
    slug: str
    name: str | None = None
    description: str | None = None
    transport: str = "auto"
    url: str | None = None
    headers: dict[str, str] | None = None
    auth_type: str = "none"
    secret_ref: str | None = None
    secret_scope: str = "project"
    auth_header_name: str | None = None
    auth_value_template: str | None = None
    options: dict[str, Any] | None = None
    enabled: bool = True


def validate_mcp_server_body(body: MCPServerBody) -> None:
    """Raise HTTPException with a user-facing detail on invalid input."""
    slug = body.slug.strip()
    if not slug or not sanitize_slug(slug):
        raise HTTPException(status_code=400, detail="slug 不能为空且须为合法标识符")
    if body.transport not in _TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"transport 仅支持 {', '.join(_TRANSPORTS)}（stdio 为 v2 预留）",
        )
    # v1 transports all need a URL; the column is nullable only for the
    # reserved stdio path.
    if not body.url:
        raise HTTPException(status_code=400, detail="url 必填（v1 仅支持 HTTP 传输）")
    if not body.url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url 必须是 http(s) 地址")
    if body.auth_type not in _AUTH_TYPES:
        raise HTTPException(status_code=400, detail=f"auth_type 仅支持 {', '.join(_AUTH_TYPES)}")
    if body.auth_type != "none" and not body.secret_ref:
        raise HTTPException(status_code=400, detail="auth_type 非 none 时须配置 secret_ref")
    if body.secret_scope not in ("project", "user"):
        raise HTTPException(status_code=400, detail="secret_scope 仅支持 project / user")
    # Sensitive header names (Authorization, X-API-Key, ...) must go through
    # auth.secret_ref — the runtime rejects them too, reject here at write.
    for name in (body.headers or {}):
        if _is_sensitive_header(name):
            raise HTTPException(
                status_code=400,
                detail=f"header {name!r} 属敏感字段，请用 auth_type/secret_ref 配置认证",
            )


def _apply_body(row: MCPServer, body: MCPServerBody) -> None:
    row.slug = body.slug.strip()
    row.name = body.name or row.slug
    row.description = body.description
    row.transport = body.transport
    row.url = body.url
    row.headers = _json_dump(body.headers)
    row.auth_type = body.auth_type
    row.secret_ref = body.secret_ref
    row.secret_scope = body.secret_scope
    row.auth_header_name = body.auth_header_name
    row.auth_value_template = body.auth_value_template
    row.options = _json_dump(body.options)
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
            raise HTTPException(status_code=409, detail=f"MCP server slug 已存在: {row.slug}")


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
        raise HTTPException(status_code=404, detail="MCP server not found")
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
