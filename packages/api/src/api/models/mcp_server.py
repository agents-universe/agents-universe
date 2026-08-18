"""MCP server registry — two-tier: global rows (project_id NULL) and
project rows (project_id set, lazily synced from the project's
``knowledge/integrations/mcp-servers.md``).

The schema is transport-agnostic: ``url`` is nullable and ``command`` /
``args`` / ``env`` are reserved for a future stdio transport.  (project_id,
slug) uniqueness is enforced at the application layer, not by a DB unique
constraint — NULL-unique semantics differ per dialect.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._compat import UTCDateTime, new_uuid as _new_uuid, now_utc as _now_utc


def _load_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class MCPServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (Index("ix_mcp_servers_project_id", "project_id"),)

    server_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"))  # NULL = global
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(Unicode(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Unicode(500))
    # auto | streamable_http | sse (stdio reserved for v2)
    transport: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    url: Mapped[str | None] = mapped_column(String(1000))
    headers: Mapped[str | None] = mapped_column(String(4000))  # JSON object, non-sensitive only
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="none")  # none | bearer | header
    secret_ref: Mapped[str | None] = mapped_column(String(100))
    secret_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project")  # project | user
    auth_header_name: Mapped[str | None] = mapped_column(String(100))
    auth_value_template: Mapped[str | None] = mapped_column(String(200))
    # JSON: allowed_hosts, allow_private_network, connect/call timeouts,
    # tools {allowlist, denylist, require_confirmation},
    # require_confirmation_for_write, redact_response
    options: Mapped[str | None] = mapped_column(String(4000))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Reserved for v2 stdio transport
    command: Mapped[str | None] = mapped_column(String(500))
    args: Mapped[str | None] = mapped_column(String(2000))  # JSON array
    env: Mapped[str | None] = mapped_column(String(4000))  # JSON object
    created_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now_utc)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def headers_dict(self) -> dict:
        return _load_json_dict(self.headers)

    @property
    def options_dict(self) -> dict:
        return _load_json_dict(self.options)
