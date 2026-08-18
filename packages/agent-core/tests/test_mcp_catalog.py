"""Tests for the MCP server registry loader (_mcp_catalog).

File-based parsing (``load_mcp_servers_from_dir``) is exercised with temp
files; the runtime reader (``load_mcp_servers``) reads the ``mcp_servers``
table via the ``mcp_registry`` fixture.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_core.tools._mcp_catalog import (
    load_mcp_servers,
    load_mcp_servers_from_dir,
    sanitize_slug,
)
from agent_core.tools.base import ToolContext


def _make_context(fs_path: str, db_session=None) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=fs_path,
        conversation_id="c1",
        user_id="u1",
        db_session=db_session,
    )


def _write_catalog(fs_path: str, content: str) -> None:
    import os

    integrations_dir = os.path.join(fs_path, "knowledge", "integrations")
    os.makedirs(integrations_dir, exist_ok=True)
    with open(os.path.join(integrations_dir, "mcp-servers.md"), "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# sanitize_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("github-copilot", "github_copilot"),
        ("My Server!", "my_server"),
        ("foo.bar", "foo_bar"),
        ("UPPER", "upper"),
        ("---leading-trailing---", "leading_trailing"),
    ],
)
def test_sanitize_slug(raw: str, expected: str) -> None:
    assert sanitize_slug(raw) == expected


# ---------------------------------------------------------------------------
# load_mcp_servers_from_dir (file parsing — used by the sync path)
# ---------------------------------------------------------------------------


def test_file_missing_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert load_mcp_servers_from_dir(Path(tmp) / "nope.md") == {}


def test_file_single_server():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
---
category: integrations
slug: integrations/mcp-servers
---

# MCP Servers

```yaml
servers:
  - slug: github-copilot
    name: GitHub MCP
    enabled: true
    transport: streamable_http
    url: https://mcp.example.com/mcp
    auth:
      type: bearer
      secret_ref: mcp:github-copilot
      secret_scope: project
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert "github_copilot" in result
        cfg = result["github_copilot"]
        assert cfg["name"] == "GitHub MCP"
        assert cfg["url"] == "https://mcp.example.com/mcp"
        assert cfg["auth"]["secret_ref"] == "mcp:github-copilot"


def test_file_slug_sanitization():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: My-Server.Name
    name: Test
    url: https://mcp.example.com
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert "my_server_name" in result
        assert "My-Server.Name" not in result


def test_file_disabled_filtered_or_listed():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: active
    name: Active
    url: https://active.example.com
  - slug: inactive
    name: Inactive
    url: https://inactive.example.com
    enabled: false
```
""")
        path = Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md"
        result = load_mcp_servers_from_dir(path)
        assert "active" in result
        assert "inactive" not in result
        # include_disabled keeps the flag so the sync/UI can show it.
        listed = load_mcp_servers_from_dir(path, include_disabled=True)
        assert listed["inactive"]["enabled"] is False


def test_file_multiple_blocks_merged():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: server-a
    name: Server A
    url: https://a.example.com
```

```yaml
servers:
  - slug: server-b
    name: Server B
    url: https://b.example.com
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert set(result.keys()) == {"server_a", "server_b"}


def test_file_duplicate_slug_overrides():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: dup
    name: First
    url: https://first.example.com
  - slug: dup
    name: Second
    url: https://second.example.com
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert len(result) == 1
        assert result["dup"]["name"] == "Second"


def test_file_bad_yaml_block_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: good
    name: Good
    url: https://good.example.com
```

```yaml
:::not valid yaml:::
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert "good" in result
        assert len(result) == 1


def test_file_entry_without_slug_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        _write_catalog(tmp, """\
```yaml
servers:
  - slug: has-slug
    name: Has Slug
    url: https://has.example.com
  - name: No Slug
    url: https://noslug.example.com
```
""")
        result = load_mcp_servers_from_dir(Path(tmp) / "knowledge" / "integrations" / "mcp-servers.md")
        assert "has_slug" in result
        assert len(result) == 1


# ---------------------------------------------------------------------------
# load_mcp_servers (runtime reader — mcp_servers table)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_empty_table_returns_empty(mcp_registry):
    session, _ = mcp_registry
    ctx = _make_context(".", session)
    assert await load_mcp_servers(ctx) == {}


@pytest.mark.asyncio
async def test_load_no_db_session_returns_empty():
    ctx = _make_context(".")
    assert await load_mcp_servers(ctx) == {}


@pytest.mark.asyncio
async def test_load_global_server(mcp_registry):
    session, seed = mcp_registry
    await seed("github-copilot", name="GitHub MCP", url="https://mcp.example.com/mcp",
               auth_type="bearer", secret_ref="mcp:github-copilot", secret_scope="user")
    ctx = _make_context(".", session)
    result = await load_mcp_servers(ctx)
    assert set(result) == {"github_copilot"}
    cfg = result["github_copilot"]
    assert cfg["name"] == "GitHub MCP"
    assert cfg["url"] == "https://mcp.example.com/mcp"
    assert cfg["auth"] == {
        "type": "bearer", "secret_ref": "mcp:github-copilot", "secret_scope": "user",
    }
    assert cfg["transport"] == "auto"


@pytest.mark.asyncio
async def test_load_project_server_scoped_to_other_project(mcp_registry):
    session, seed = mcp_registry
    await seed("github-copilot", project_id="other-project")
    ctx = _make_context(".", session)  # project p1
    assert await load_mcp_servers(ctx) == {}


@pytest.mark.asyncio
async def test_load_project_shadows_global(mcp_registry):
    session, seed = mcp_registry
    await seed("github-copilot", project_id=None, name="Global", url="https://global.example.com")
    await seed("github-copilot", project_id="p1", name="Project", url="https://project.example.com")
    ctx = _make_context(".", session)
    result = await load_mcp_servers(ctx)
    assert set(result) == {"github_copilot"}
    # Project entry wins over the global one with the same slug.
    assert result["github_copilot"]["name"] == "Project"
    assert result["github_copilot"]["url"] == "https://project.example.com"


@pytest.mark.asyncio
async def test_load_global_visible_when_no_project_shadow(mcp_registry):
    session, seed = mcp_registry
    await seed("github-copilot", project_id=None, name="Global")
    ctx = _make_context(".", session)
    result = await load_mcp_servers(ctx)
    assert result["github_copilot"]["name"] == "Global"


@pytest.mark.asyncio
async def test_load_disabled_filtered(mcp_registry):
    session, seed = mcp_registry
    await seed("active", enabled=1)
    await seed("inactive", enabled=0)
    ctx = _make_context(".", session)
    assert set(await load_mcp_servers(ctx)) == {"active"}


@pytest.mark.asyncio
async def test_load_options_and_headers_round_trip(mcp_registry):
    session, seed = mcp_registry
    await seed(
        "filtered",
        headers='{"X-Team": "platform"}',
        options='{"allow_private_network": true, "tools": {"allowlist": ["echo"]}, "redact_response": false}',
    )
    ctx = _make_context(".", session)
    cfg = (await load_mcp_servers(ctx))["filtered"]
    assert cfg["headers"] == {"X-Team": "platform"}
    assert cfg["allow_private_network"] is True
    assert cfg["tools"] == {"allowlist": ["echo"]}
    assert cfg["redact_response"] is False
