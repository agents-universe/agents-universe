"""Project-scoped MCP server sync (file → table).

MCP servers have two tiers (see ``_mcp_catalog``): global rows in the
``mcp_servers`` table managed via REST CRUD, and project rows whose source of
truth is the project's ``knowledge/integrations/mcp-servers.md`` file.  This
module lazily syncs the file into the table — the integration expert's file
writes take effect without a restart, mirroring ``agent_sync`` for project
agents.

Scope semantics: only rows with ``project_id == <pid>`` are managed.  Global
rows are never touched, and a missing catalog file means no project servers
(the file is the single source for the project tier).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.tools._mcp_catalog import (
    _CFG_COLUMN_KEYS,
    _CFG_OPTION_KEYS,
    load_mcp_servers_from_dir,
    sanitize_slug,
)

from api.models.mcp_server import MCPServer

log = logging.getLogger("agents_universe.mcp_sync")


def _j(v) -> str | None:
    return json.dumps(v, ensure_ascii=False) if v else None


def _entry_to_attrs(entry: dict) -> dict:
    """Map a catalog-file entry onto mcp_servers column values.

    Symmetric with ``mcp_row_to_config`` in agent-core: dedicated columns for
    the core/auth keys, everything else the runtime reads (allowed_hosts,
    tools filters, timeouts, ...) into the options JSON column.
    """
    slug = str(entry.get("slug", "")).strip()
    auth = entry.get("auth")
    auth = auth if isinstance(auth, dict) else {}

    attrs: dict = {
        "slug": slug,
        "name": entry.get("name") or slug,
        "description": entry.get("description"),
        "transport": entry.get("transport") or "auto",
        "url": entry.get("url"),
        "headers": _j(entry.get("headers") if isinstance(entry.get("headers"), dict) else None),
        "auth_type": (auth.get("type") or "none").lower(),
        "secret_ref": auth.get("secret_ref"),
        "secret_scope": (auth.get("secret_scope") or "project").lower(),
        "auth_header_name": auth.get("header_name"),
        "auth_value_template": auth.get("value_template"),
        "options": _j({k: entry[k] for k in _CFG_OPTION_KEYS if k in entry}),
        "enabled": bool(entry.get("enabled", True)),
    }
    # Keys unknown to both the column list and the options list (typos etc.)
    # are dropped silently — the runtime would ignore them anyway.
    return attrs


async def sync_mcp_servers_from_file(
    session: AsyncSession,
    project_id: str,
    catalog_path: Path,
) -> tuple[list[str], list[str]]:
    """Upsert a project's MCP servers from its catalog file into the table.

    Manages only rows with ``project_id == project_id``.  Rows whose slug
    (sanitized, matching the runtime key) is no longer in the file are
    deleted — a missing file clears the project tier.  Returns
    ``(synced, removed)`` slug lists.
    """
    entries = await asyncio.to_thread(
        load_mcp_servers_from_dir, catalog_path, True
    )
    synced: list[str] = []
    existing: dict[str, MCPServer] = {}
    result = await session.execute(
        select(MCPServer).where(MCPServer.project_id == project_id)
    )
    for row in result.scalars().all():
        existing[sanitize_slug(row.slug)] = row

    for slug, entry in entries.items():
        attrs = _entry_to_attrs(entry)
        row = existing.get(slug)
        try:
            if row is None:
                row = MCPServer(project_id=project_id, **attrs)
                session.add(row)
                log.info("Registered project MCP server: %s", slug)
            else:
                for k, v in attrs.items():
                    setattr(row, k, v)
            synced.append(slug)
        except Exception:
            log.exception("Failed to sync MCP server %s", slug)

    removed: list[str] = []
    for slug, row in existing.items():
        if slug not in entries:
            await session.delete(row)
            removed.append(row.slug)
            log.info("Removed stale project MCP server: %s", row.slug)

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception("Failed to commit MCP server sync for project %s", project_id)
    return synced, removed
