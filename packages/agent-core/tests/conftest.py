"""Shared fixtures: in-memory ``mcp_servers`` table for registry tests.

agent-core reads the MCP registry from the ``mcp_servers`` table via raw SQL
(``_mcp_catalog.load_mcp_servers``), so tests that exercise the runtime path
need a minimal schema + a seeding helper.  Mirrors the episodic_memories
pattern from test_memory_rw.py.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_MCP_SERVERS_DDL = """
CREATE TABLE mcp_servers (
    server_id TEXT PRIMARY KEY,
    project_id TEXT,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    transport TEXT NOT NULL DEFAULT 'auto',
    url TEXT,
    headers TEXT,
    auth_type TEXT NOT NULL DEFAULT 'none',
    secret_ref TEXT,
    secret_scope TEXT NOT NULL DEFAULT 'project',
    auth_header_name TEXT,
    auth_value_template TEXT,
    options TEXT,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    command TEXT,
    args TEXT,
    env TEXT,
    created_by TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

_INSERT = """
INSERT INTO mcp_servers (server_id, project_id, slug, name, transport, url,
    headers, auth_type, secret_ref, secret_scope, auth_header_name,
    auth_value_template, options, enabled)
VALUES (:server_id, :project_id, :slug, :name, :transport, :url, :headers,
    :auth_type, :secret_ref, :secret_scope, :auth_header_name,
    :auth_value_template, :options, :enabled)
"""


@pytest.fixture
async def mcp_registry():
    """(session, seed) — session holds the mcp_servers table; seed(slug, **kw)
    inserts a row with sensible defaults and returns nothing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_MCP_SERVERS_DDL)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        import uuid

        async def seed(slug: str, **kw):
            import uuid

            name = kw.pop("name", slug)
            values = {
                "server_id": str(uuid.uuid4()),
                "project_id": None,
                "slug": slug,
                "name": name,
                "transport": "auto",
                "url": "https://mcp.example.com/mcp",
                "headers": None,
                "auth_type": "none",
                "secret_ref": None,
                "secret_scope": "project",
                "auth_header_name": None,
                "auth_value_template": None,
                "options": None,
                "enabled": 1,
            }
            values.update(kw)
            await session.execute(text(_INSERT), values)
            await session.commit()

        yield session, seed
    await engine.dispose()
