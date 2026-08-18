"""Tests for GET /api/projects/{project_id}/mcp-servers — project MCP
registry surfaced to the integrations UI (metadata only, never secrets).

The endpoint lazily syncs the project's ``integrations/mcp-servers.md`` file
into the ``mcp_servers`` table, then lists project rows (scope=project) plus
global rows (scope=global, project_id NULL) with the same slug shadowed by
the project entry.
"""
from __future__ import annotations

from api.paths import PROJECTS_ROOT

CATALOG = """---
slug: integrations/mcp-servers
---
```yaml
servers:
  - slug: github-copilot
    name: GitHub Copilot
    enabled: true
    url: https://mcp.example.com/github
    auth:
      type: bearer
      secret_ref: mcp:github-copilot
      secret_scope: project
  - slug: placeholder
    name: Placeholder
    enabled: false
    url: https://mcp.example.com/placeholder
  - slug: headers-srv
    name: Headers Server
    enabled: true
    url: https://mcp.example.com/headers
    headers:
      X-Team: platform
      Authorization: Bearer should-never-leak
```
"""


async def _write_catalog(project) -> None:
    knowledge = PROJECTS_ROOT / project.slug / "knowledge" / "integrations"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "mcp-servers.md").write_text(CATALOG, encoding="utf-8")


async def test_list_mcp_servers_metadata_only(client, make_project):
    project = await make_project()
    await _write_catalog(project)

    resp = await client.get(f"/api/projects/{project.project_id}/mcp-servers")
    assert resp.status_code == 200
    servers = {s["slug"]: s for s in resp.json()}
    # Global rows created by other tests may share the DB — the file's three
    # project entries must be present and win their slugs.
    assert {"github-copilot", "placeholder", "headers-srv"} <= set(servers)

    gh = servers["github-copilot"]
    assert gh["name"] == "GitHub Copilot"
    assert gh["url"] == "https://mcp.example.com/github"
    assert gh["enabled"] is True
    assert gh["auth_type"] == "bearer"
    assert gh["secret_ref"] == "mcp:github-copilot"
    assert gh["has_secret"] is False
    # File-synced rows are project-scoped — the UI renders the badge.
    assert gh["scope"] == "project"
    assert gh["server_id"]

    # Disabled servers are listed with their flag so the UI can show them.
    assert servers["placeholder"]["enabled"] is False

    # Sensitive catalog fields must never leave the server.
    raw = resp.text
    assert "should-never-leak" not in raw
    assert "X-Team" not in raw
    assert "Authorization" not in raw


async def test_list_mcp_servers_secret_configured(client, make_project):
    project = await make_project()
    await _write_catalog(project)
    resp = await client.post(
        f"/api/projects/{project.project_id}/secrets",
        json={"service_key": "mcp:github-copilot", "value": "ghp_supersecret"},
    )
    assert resp.status_code == 201

    resp = await client.get(f"/api/projects/{project.project_id}/mcp-servers")
    servers = {s["slug"]: s for s in resp.json()}
    assert servers["github-copilot"]["has_secret"] is True
    assert "ghp_supersecret" not in resp.text


async def test_list_mcp_servers_missing_catalog_empty(client, make_project):
    """No catalog file → no project-scoped rows (global rows may remain
    from other tests in the shared DB)."""
    project = await make_project()
    resp = await client.get(f"/api/projects/{project.project_id}/mcp-servers")
    assert resp.status_code == 200
    assert not any(s["scope"] == "project" for s in resp.json())


async def test_list_includes_global_servers(client, make_project, db):
    """Global rows (project_id NULL) show up with scope=global; a project
    row with the same slug shadows the global one."""
    project = await make_project()
    from api.models.mcp_server import MCPServer

    db.add(MCPServer(
        slug="github-copilot", name="Global GitHub", url="https://global.example.com",
    ))
    db.add(MCPServer(
        slug="standalone", name="Standalone Global", url="https://standalone.example.com",
    ))
    await db.commit()
    await _write_catalog(project)  # file defines a project github-copilot

    resp = await client.get(f"/api/projects/{project.project_id}/mcp-servers")
    assert resp.status_code == 200
    servers = {s["slug"]: s for s in resp.json()}

    # Project entry shadows the global one with the same slug.
    gh = servers["github-copilot"]
    assert gh["scope"] == "project"
    assert gh["url"] == "https://mcp.example.com/github"
    assert gh["name"] == "GitHub Copilot"

    # Unshadowed global rows are visible with their badge.
    standalone = servers["standalone"]
    assert standalone["scope"] == "global"
    assert standalone["url"] == "https://standalone.example.com"
