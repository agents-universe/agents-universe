"""Tests for the global MCP server registry CRUD + test endpoint.

Global servers (``project_id`` NULL) are cross-project; project-scoped
servers are file-synced and never managed here.  Metadata responses never
leak headers or secret values.
"""
from __future__ import annotations

import uuid

import pytest


def _slug() -> str:
    """Unique per-test slug — the shared test DB persists across tests."""
    return f"mcp-{uuid.uuid4().hex[:8]}"


async def _create(client, **overrides):
    body = {
        "slug": _slug(),
        "name": "Test Server",
        "url": "https://mcp.example.com/mcp",
        "transport": "auto",
        "auth_type": "none",
        "enabled": True,
        **overrides,
    }
    return await client.post("/api/mcp/servers", json=body)


async def test_create_list_update_delete(client):
    # Create.
    resp = await _create(client)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["scope"] == "global"
    assert created["has_secret"] is False
    server_id = created["server_id"]
    slug = created["slug"]

    # List.
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 200
    listing = {s["slug"]: s for s in resp.json()}
    assert listing[slug]["server_id"] == server_id
    assert listing[slug]["scope"] == "global"

    # Update.
    resp = await client.put(
        f"/api/mcp/servers/{server_id}",
        json={
            "slug": slug,
            "name": "Test Server (prod)",
            "url": "https://mcp.example.com/mcp",
            "transport": "streamable_http",
            "auth_type": "none",
            "enabled": False,
        },
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["name"] == "Test Server (prod)"
    assert updated["transport"] == "streamable_http"
    assert updated["enabled"] is False

    # Delete.
    resp = await client.delete(f"/api/mcp/servers/{server_id}")
    assert resp.status_code == 204
    resp = await client.get("/api/mcp/servers")
    assert slug not in {s["slug"] for s in resp.json()}


async def test_create_duplicate_slug_conflict(client):
    slug = _slug()
    assert (await _create(client, slug=slug)).status_code == 201
    resp = await _create(client, slug=slug)
    assert resp.status_code == 409


async def test_create_sanitized_slug_collision_conflict(client):
    """Two raw slugs that sanitize to the same runtime key are duplicates."""
    base = _slug()
    assert (await _create(client, slug=base)).status_code == 201
    resp = await _create(client, slug=base.replace("-", "_"))
    assert resp.status_code == 409


async def test_create_requires_url(client):
    resp = await _create(client, url=None)
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"]


async def test_create_rejects_non_http_url(client):
    resp = await _create(client, url="ftp://mcp.example.com")
    assert resp.status_code == 400


async def test_create_rejects_reserved_transport(client):
    resp = await _create(client, transport="stdio")
    assert resp.status_code == 400
    assert "v2" in resp.json()["detail"]


async def test_create_auth_requires_secret_ref(client):
    resp = await _create(client, auth_type="bearer", secret_ref=None)
    assert resp.status_code == 400


async def test_create_rejects_sensitive_header(client):
    resp = await _create(client, headers={"Authorization": "Bearer xyz"})
    assert resp.status_code == 400
    assert "secret_ref" in resp.json()["detail"]


async def test_create_accepts_non_sensitive_headers(client):
    resp = await _create(client, headers={"X-Team": "platform"})
    assert resp.status_code == 201
    # Headers never serialize back to the client.
    assert "X-Team" not in resp.text


async def test_create_stores_options_and_has_secret_flag(client, db):
    secret_ref = f"mcp:{_slug()}"
    resp = await _create(
        client,
        auth_type="bearer",
        secret_ref=secret_ref,
        secret_scope="user",
        options={"allow_private_network": True, "tools": {"denylist": ["delete_*"]}},
    )
    assert resp.status_code == 201
    created = resp.json()
    slug = created["slug"]
    assert created["has_secret"] is False  # nothing stored yet

    # Save a user token under the secret_ref, then the flag flips.
    from api.models.user import UserToken

    db.add(UserToken(
        user_id="test-user",
        service_key=secret_ref,
        encrypted_value="ciphertext",
    ))
    await db.commit()

    resp = await client.get("/api/mcp/servers")
    entry = next(s for s in resp.json() if s["slug"] == slug)
    assert entry["has_secret"] is True
    assert entry["tool_denylist"] == ["delete_*"]


async def test_update_missing_404(client):
    resp = await client.put(
        "/api/mcp/servers/nope",
        json={"slug": "x", "url": "https://mcp.example.com", "auth_type": "none"},
    )
    assert resp.status_code == 404


async def test_delete_missing_404(client):
    resp = await client.delete("/api/mcp/servers/nope")
    assert resp.status_code == 404


async def test_test_connection_success(client, mcp_test_server_url, monkeypatch):
    """Test endpoint reuses the runtime connect path — success reports tools."""
    server_id = (await _create(client, url=mcp_test_server_url,
                               options={"allow_private_network": True})).json()["server_id"]
    resp = await client.post(f"/api/mcp/servers/{server_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["tools"] >= 3


async def test_test_connection_failure_is_safe(client, monkeypatch):
    """Unreachable server → ok=False with a short, non-leaking error.

    127.0.0.1 is blocked by the runtime URL safety check unless
    allow_private_network is set — this exercises the safety gate quickly
    without touching the network.
    """
    server_id = (await _create(client, url="http://127.0.0.1:1/mcp")).json()["server_id"]
    resp = await client.post(f"/api/mcp/servers/{server_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert len(body.get("error", "")) <= 300


@pytest.fixture
async def mcp_test_server_url():
    """Start a real MCP streamable-http server and return its URL."""
    import asyncio
    import socket

    import uvicorn
    from mcp import types
    from mcp.server.lowlevel import Server

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def on_list_tools(ctx, params=None):
        return types.ListToolsResult(tools=[
            types.Tool(name="echo", description="Echo", input_schema={
                "type": "object", "properties": {"text": {"type": "string"}},
            }),
            types.Tool(name="adder", description="Add", input_schema={
                "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            }),
            types.Tool(name="delete_thing", description="Delete", input_schema={"type": "object"}),
        ])

    server = Server("test-server", on_list_tools=on_list_tools)
    app = server.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server_instance = uvicorn.Server(config)

    task = asyncio.create_task(server_instance.serve())
    await asyncio.sleep(0.5)

    yield f"http://127.0.0.1:{port}/mcp"

    server_instance.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError:
        task.cancel()
