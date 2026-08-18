"""MCP server registry loader.

Two-tier registry (see ``knowledge/_template/mcp-servers.md``):
- **Global** servers live in the ``mcp_servers`` table (``project_id`` NULL),
  managed through the REST CRUD endpoints + settings UI.
- **Project** servers live in the project's
  ``integrations/mcp-servers`` knowledge file and are lazily synced into the
  same table by ``api/services/mcp_sync.py`` (the integration expert writes
  the file; the sync is a projection like project agents).

The runtime reads ONLY the table: ``load_mcp_servers`` queries
``project_id = :current OR project_id IS NULL`` and lets a project row
shadow a global row with the same sanitized slug (mirroring the
agent/skill/workflow shadowing rule).  ``load_mcp_servers_from_dir`` remains
for the sync path (file → table) and offline tests.

Parsing is deliberately permissive: a malformed catalog block never breaks
the whole file, and a missing/unreadable file yields an empty dict.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from sqlalchemy import text

from .base import ToolContext

_log = logging.getLogger(__name__)

MCP_CATALOG_SLUG = "integrations/mcp-servers"

_YAML_FENCE_RE = re.compile(
    r"^```ya?ml\s*\n(.*?)```", re.DOTALL | re.MULTILINE | re.IGNORECASE
)

_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]")

# Catalog entry keys mapped to dedicated mcp_servers columns vs the options
# JSON column.  Keep the two lists symmetric with api/services/mcp_sync.py.
_CFG_COLUMN_KEYS = frozenset({
    "slug", "name", "description", "transport", "url", "headers", "enabled",
})
_CFG_OPTION_KEYS = frozenset({
    "allowed_hosts", "allow_private_network", "connect_timeout_seconds",
    "call_timeout_seconds", "tools", "require_confirmation_for_write",
    "redact_response",
})
_CFG_AUTH_KEYS = frozenset({"type", "secret_ref", "secret_scope", "header_name", "value_template"})


def sanitize_slug(slug: str) -> str:
    """Normalise a server slug into a valid tool-name fragment."""
    return _SLUG_SANITIZE_RE.sub("_", slug.lower()).strip("_")


def _load_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def mcp_row_to_config(row: Any) -> dict[str, Any]:
    """Convert a ``mcp_servers`` table row into the runtime config dict.

    The result shape matches what ``load_mcp_servers_from_dir`` used to
    produce for the same entry, so the consumer (mcp_client) is agnostic of
    whether a server came from a global row, a synced project row, or a file.
    """
    auth_type = (getattr(row, "auth_type", None) or "none").lower()
    auth: dict[str, Any] = {}
    if auth_type != "none":
        auth = {
            "type": auth_type,
            "secret_ref": getattr(row, "secret_ref", None),
            "secret_scope": getattr(row, "secret_scope", None) or "project",
        }
        if getattr(row, "auth_header_name", None):
            auth["header_name"] = row.auth_header_name
        if getattr(row, "auth_value_template", None):
            auth["value_template"] = row.auth_value_template

    cfg: dict[str, Any] = {
        "slug": getattr(row, "slug", ""),
        "name": getattr(row, "name", "") or getattr(row, "slug", ""),
        "transport": getattr(row, "transport", None) or "auto",
        "url": getattr(row, "url", None),
        "headers": _load_json_dict(getattr(row, "headers", None)),
        "auth": auth,
        "enabled": bool(getattr(row, "enabled", True)),
    }
    description = getattr(row, "description", None)
    if description:
        cfg["description"] = description
    cfg.update(_load_json_dict(getattr(row, "options", None)))
    return cfg


_MCP_TABLE_SELECT = (
    "SELECT project_id, slug, name, description, transport, url, headers, "
    "auth_type, secret_ref, secret_scope, auth_header_name, auth_value_template, "
    "options, enabled "
    "FROM mcp_servers"
)


async def load_mcp_servers(context: ToolContext) -> dict[str, dict[str, Any]]:
    """Load enabled MCP servers for the context's project from the table.

    Returns ``{sanitized_slug: server_config}``.  Project rows shadow global
    rows with the same sanitized slug; later project rows override earlier
    ones (mirroring the catalog file's duplicate-slug rule).
    """
    if not context.db_session:
        _log.warning("MCP catalog load skipped: no database session on context")
        return {}

    result = await context.db_session.execute(
        text(_MCP_TABLE_SELECT + " WHERE (project_id = :pid OR project_id IS NULL) AND enabled = :enabled"),
        {"pid": context.project_id, "enabled": True},
    )

    global_cfgs: dict[str, dict[str, Any]] = {}
    project_cfgs: dict[str, dict[str, Any]] = {}
    for row in result.fetchall():
        key = sanitize_slug(row.slug)
        if not key:
            continue
        if row.project_id is None:
            global_cfgs.setdefault(key, mcp_row_to_config(row))
        else:
            project_cfgs[key] = mcp_row_to_config(row)

    servers = dict(global_cfgs)
    servers.update(project_cfgs)  # project entries shadow global ones
    return servers


def load_mcp_servers_from_dir(
    catalog_path: Path, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load and index the MCP server catalog from a catalog file.

    Returns ``{sanitized_slug: server_config}``. Disabled servers are
    skipped unless ``include_disabled`` is set (used by the sync so disabled
    entries keep their flag in the table, and by the API so the UI can show
    them). Missing file, read errors, and per-block parse failures are logged
    and skipped - this function never raises and never returns None.

    Kept sync so non-agent callers (the mcp_sync service) can reuse the exact
    same parser; ``load_mcp_servers`` is the agent-facing table reader.
    """
    if not catalog_path.exists():
        _log.debug("MCP catalog not found at %s", catalog_path)
        return {}

    try:
        content = catalog_path.read_text("utf-8")
    except OSError as exc:
        _log.warning("MCP catalog read failed: %s", exc)
        return {}

    # Strip YAML frontmatter (the file may or may not have one).
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("MCP catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    servers: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("MCP catalog YAML block skipped: %s", exc)
            continue
        if not isinstance(block, dict):
            continue
        raw_servers = block.get("servers")
        if not isinstance(raw_servers, list):
            continue
        for entry in raw_servers:
            if not isinstance(entry, dict) or not entry.get("slug"):
                _log.debug("MCP server entry without slug skipped: %r", entry)
                continue
            slug = sanitize_slug(str(entry["slug"]))
            if not slug:
                continue
            if slug in servers:
                _log.warning("MCP server slug %r duplicated; later entry overrides", slug)
            servers[slug] = entry

    # Filter disabled servers in-place (unless the caller wants them listed).
    if not include_disabled:
        disabled = [s for s, cfg in servers.items() if not cfg.get("enabled", True)]
        for s in disabled:
            _log.debug("MCP server %r disabled, skipping", s)
            servers.pop(s, None)

    return servers
