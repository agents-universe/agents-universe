"""Tests for the MCP client integration (mcp_client.py).

Unit tests use mock objects to avoid network dependencies.  One integration
test starts a real MCP streamable-http server via ``mcp.server.lowlevel.Server``.
"""
from __future__ import annotations

import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.mcp_client import (
    McpConnectError,
    McpConnectionManager,
    McpProxyTool,
    McpServerSession,
    _matches_any,
    _validate_mcp_url,
    attach_mcp_tools,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(fs_path: str | None = None, session=None, db_session=None) -> ToolContext:
    if fs_path is None:
        fs_path = tempfile.mkdtemp()
    return ToolContext(
        project_id="p1",
        project_fs_path=fs_path,
        conversation_id="c1",
        user_id="u1",
        session=session,
        db_session=db_session,
    )


def _make_mcp_tool(
    name: str = "echo",
    description: str = "Echo back the input",
    input_schema: dict | None = None,
    destructive: bool = False,
):
    """Create a mock object that quacks like mcp.types.Tool."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.input_schema = input_schema or {"type": "object", "properties": {"text": {"type": "string"}}}
    if destructive:
        annotations = MagicMock()
        annotations.destructive_hint = True
        tool.annotations = annotations
    else:
        tool.annotations = None
    return tool


def _make_text_result(text: str, is_error: bool = False):
    """Create a mock CallToolResult with text content."""
    result = MagicMock()
    result.is_error = is_error
    block = MagicMock()
    block.type = "text"
    block.text = text
    result.content = [block]
    return result


# ---------------------------------------------------------------------------
# _validate_mcp_url
# ---------------------------------------------------------------------------


def test_validate_url_ok():
    assert _validate_mcp_url("https://mcp.example.com/mcp", None, False) is None


def test_validate_url_bad_scheme():
    err = _validate_mcp_url("ftp://mcp.example.com", None, False)
    assert err is not None and "Scheme" in err


def test_validate_url_private_ip_blocked():
    err = _validate_mcp_url("http://127.0.0.1:8080/mcp", None, False)
    assert err is not None and "blocked range" in err


def test_validate_url_private_ip_allowed():
    assert _validate_mcp_url("http://127.0.0.1:8080/mcp", None, True) is None


def test_validate_url_allowed_hosts_mismatch():
    err = _validate_mcp_url("https://evil.com/mcp", ["mcp.example.com"], False)
    assert err is not None and "not in allowed_hosts" in err


# ---------------------------------------------------------------------------
# _matches_any
# ---------------------------------------------------------------------------


def test_matches_any_exact():
    assert _matches_any("search", ["search"]) is True


def test_matches_any_glob():
    assert _matches_any("search_issues", ["search_*"]) is True


def test_matches_any_no_match():
    assert _matches_any("get_issue", ["search_*"]) is False


def test_matches_any_empty_patterns():
    assert _matches_any("anything", []) is False


# ---------------------------------------------------------------------------
# McpProxyTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_tool_name_and_description():
    tool = _make_mcp_tool(name="echo", description="Echo the input")
    proxy = McpProxyTool(
        name="mcp__test__echo",
        server_slug="test",
        server_name="Test Server",
        orig_tool_name="echo",
        mcp_tool=tool,
        server_cfg={},
        manager=MagicMock(),
    )
    assert proxy.name == "mcp__test__echo"
    assert "[MCP:Test Server]" in proxy.description
    assert "Echo the input" in proxy.description


@pytest.mark.asyncio
async def test_proxy_tool_parameters_normalization():
    # When input_schema is not a dict, should return a default object schema.
    tool = MagicMock()
    tool.input_schema = None
    proxy = McpProxyTool("mcp__t__x", "t", "T", "x", tool, {}, MagicMock())
    assert proxy.parameters == {"type": "object", "properties": {}}

    # When it is a dict, pass through.
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    tool2 = _make_mcp_tool(input_schema=schema)
    proxy2 = McpProxyTool("mcp__t__x", "t", "T", "x", tool2, {}, MagicMock())
    assert proxy2.parameters == schema


@pytest.mark.asyncio
async def test_proxy_tool_execute_text():
    tool = _make_mcp_tool()
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("hello world"))
    manager.get_session = AsyncMock(return_value=session)

    proxy = McpProxyTool("mcp__t__echo", "t", "Test", "echo", tool, {}, manager)
    ctx = _make_context()
    result = await proxy.execute({"text": "hello"}, ctx)
    assert result["content"] == "hello world"
    assert "error" not in result
    # The mock session is called directly (McpServerSession.call_tool wraps the
    # SDK call with read_timeout_seconds; that path is covered by the
    # integration tests against the real SDK).
    session.call_tool.assert_called_once_with("echo", {"text": "hello"})


@pytest.mark.asyncio
async def test_proxy_tool_execute_error():
    tool = _make_mcp_tool()
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("Something went wrong", is_error=True))
    manager.get_session = AsyncMock(return_value=session)

    proxy = McpProxyTool("mcp__t__echo", "t", "Test", "echo", tool, {}, manager)
    ctx = _make_context()
    result = await proxy.execute({}, ctx)
    assert result["error"] == "Something went wrong"


@pytest.mark.asyncio
async def test_proxy_tool_execute_timeout():
    tool = _make_mcp_tool()
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=TimeoutError())
    manager.get_session = AsyncMock(return_value=session)

    proxy = McpProxyTool("mcp__t__echo", "t", "Test", "echo", tool, {}, manager)
    ctx = _make_context()
    result = await proxy.execute({}, ctx)
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_proxy_tool_execute_no_session():
    tool = _make_mcp_tool()
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=None)

    proxy = McpProxyTool("mcp__t__echo", "t", "Test", "echo", tool, {}, manager)
    ctx = _make_context()
    result = await proxy.execute({}, ctx)
    assert "not connected" in result["error"]


@pytest.mark.asyncio
async def test_proxy_tool_redaction():
    tool = _make_mcp_tool()
    manager = MagicMock()
    session = AsyncMock()
    # Simulate a result that contains a sensitive key in the text (won't be redacted
    # since redaction works on dict keys, not text content).  Test dict redaction
    # by checking the output dict structure.
    session.call_tool = AsyncMock(return_value=_make_text_result('{"api_key": "secret123"}'))
    manager.get_session = AsyncMock(return_value=session)

    cfg = {"redact_response": True}
    proxy = McpProxyTool("mcp__t__echo", "t", "Test", "echo", tool, cfg, manager)
    ctx = _make_context()
    result = await proxy.execute({}, ctx)
    # Text content is not a dict, so it passes through.  But the output dict itself
    # has no sensitive keys, so nothing changes.
    assert result["content"] == '{"api_key": "secret123"}'


@pytest.mark.asyncio
async def test_proxy_tool_confirmation_destructive():
    """Tools with destructiveHint should trigger confirmation."""
    tool = _make_mcp_tool(name="delete_item", destructive=True)
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("deleted"))
    manager.get_session = AsyncMock(return_value=session)

    fake_session = AsyncMock()
    fake_session.request_user_selection = AsyncMock(return_value="allow")

    cfg = {"require_confirmation_for_write": True}
    proxy = McpProxyTool("mcp__t__delete", "t", "Test", "delete_item", tool, cfg, manager)
    ctx = _make_context(session=fake_session)
    result = await proxy.execute({"id": "123"}, ctx)
    assert result["content"] == "deleted"
    fake_session.request_user_selection.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_tool_confirmation_denied():
    """When user denies, the tool should not be called."""
    tool = _make_mcp_tool(name="delete_item", destructive=True)
    manager = MagicMock()
    session = AsyncMock()
    manager.get_session = AsyncMock(return_value=session)

    fake_session = AsyncMock()
    fake_session.request_user_selection = AsyncMock(return_value="deny")

    cfg = {"require_confirmation_for_write": True}
    proxy = McpProxyTool("mcp__t__delete", "t", "Test", "delete_item", tool, cfg, manager)
    ctx = _make_context(session=fake_session)
    result = await proxy.execute({"id": "123"}, ctx)
    assert "error" in result
    assert "declined" in result["error"]
    session.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_tool_confirmation_pattern():
    """require_confirmation glob patterns should trigger confirmation."""
    tool = _make_mcp_tool(name="merge_branch", destructive=False)
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("merged"))
    manager.get_session = AsyncMock(return_value=session)

    fake_session = AsyncMock()
    fake_session.request_user_selection = AsyncMock(return_value="allow")

    cfg = {"tools": {"require_confirmation": ["merge_*"]}}
    proxy = McpProxyTool("mcp__t__merge", "t", "Test", "merge_branch", tool, cfg, manager)
    ctx = _make_context(session=fake_session)
    result = await proxy.execute({"branch": "main"}, ctx)
    assert result["content"] == "merged"
    fake_session.request_user_selection.assert_called_once()


@pytest.mark.asyncio
async def test_proxy_tool_no_confirmation_when_not_destructive():
    """Non-destructive tools without confirmation patterns should not prompt."""
    tool = _make_mcp_tool(name="get_issue", destructive=False)
    manager = MagicMock()
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_make_text_result("issue data"))
    manager.get_session = AsyncMock(return_value=session)

    fake_session = AsyncMock()
    fake_session.request_user_selection = AsyncMock()

    proxy = McpProxyTool("mcp__t__get", "t", "Test", "get_issue", tool, {}, manager)
    ctx = _make_context(session=fake_session)
    result = await proxy.execute({"id": "1"}, ctx)
    assert result["content"] == "issue data"
    fake_session.request_user_selection.assert_not_called()


# ---------------------------------------------------------------------------
# attach_mcp_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_no_declaration_returns_empty():
    ctx = _make_context()
    result = await attach_mcp_tools(ctx, ["shell", "web_fetch"])
    assert result == {}


@pytest.mark.asyncio
async def test_attach_no_db_session_returns_empty():
    """The registry lives in the DB — without a session nothing can load."""
    ctx = _make_context(tempfile.mkdtemp())
    result = await attach_mcp_tools(ctx, ["mcp"])
    assert result == {}


@pytest.mark.asyncio
async def test_attach_parses_declarations(mcp_registry):
    """Test that mcp and mcp:slug declarations are parsed correctly."""
    session, seed = mcp_registry
    await seed("server-a", name="Server A")
    await seed("server-b", name="Server B")
    ctx = _make_context(tempfile.mkdtemp(), db_session=session)

    # Mock the discover_tools to avoid actual connections.
    with patch.object(McpConnectionManager, "discover_tools", new_callable=AsyncMock) as mock_discover:
        mock_discover.return_value = {"mcp__server_a__tool1": MagicMock()}
        result = await attach_mcp_tools(ctx, ["mcp"])
        assert "mcp__server_a__tool1" in result
        # discover_tools called with target_slugs=None (all servers)
        call_args = mock_discover.call_args
        assert call_args[0][1] is None  # target_slugs=None

        # Specific server declaration
        mock_discover.reset_mock()
        mock_discover.return_value = {}
        await attach_mcp_tools(ctx, ["mcp:server-a"])
        call_args = mock_discover.call_args
        assert call_args[0][1] == {"server_a"}


# ---------------------------------------------------------------------------
# discover_tools robustness (bad config isolation, unknown transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_isolates_bad_server_config():
    """A non-McpConnectError from one server (e.g. bad connect_timeout_seconds
    value) must not abort discovery of the other servers, and every connected
    session must be registered so close_all() can clean it up."""
    servers = {
        "good": {"url": "https://good.example.com/mcp"},
        "bad": {"url": "https://bad.example.com/mcp", "connect_timeout_seconds": "not-a-number"},
    }
    ctx = _make_context()
    manager = McpConnectionManager(ctx)

    async def fake_start(self):
        if self.slug == "bad":
            # Mirrors the ValueError raised by float() on a malformed value.
            raise ValueError("invalid connect_timeout_seconds")
        self._session = MagicMock()
        self._tools = [_make_mcp_tool(name="echo")]
        self._ready.set()

    with patch.object(McpServerSession, "start", fake_start):
        tools = await manager.discover_tools(servers)

    # Tools from the healthy server survive; the broken one is skipped.
    assert set(tools) == {"mcp__good__echo"}

    # No session leak: only "good" is registered and close_all closes it.
    assert set(manager._sessions) == {"good"}
    good = manager._sessions["good"]
    good.close = AsyncMock()
    await manager.close_all()
    good.close.assert_awaited_once()
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_unknown_transport_fails_fast():
    """An unrecognized transport value must error immediately instead of
    hanging until the connect timeout."""
    session = McpServerSession("x", {"url": "https://x.example.com/mcp", "transport": "bogus"}, {})
    with pytest.raises(McpConnectError, match="Unknown MCP transport"):
        await asyncio.wait_for(session.start(), timeout=5)
    await session.close()


# ---------------------------------------------------------------------------
# Integration test: real MCP server via streamable_http
# ---------------------------------------------------------------------------


@pytest.fixture
async def mcp_test_server():
    """Start a real MCP streamable-http server on a random port."""
    import socket
    from mcp.server.lowlevel import Server
    from mcp import types

    # Find a free port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def on_list_tools(ctx, params=None):
        return types.ListToolsResult(tools=[
            types.Tool(
                name="echo",
                description="Echo back the input text",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            ),
            types.Tool(
                name="adder",
                description="Add two numbers",
                input_schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]},
            ),
            types.Tool(
                name="delete_thing",
                description="Delete something destructive",
                input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                annotations=types.ToolAnnotations(destructive_hint=True),
            ),
        ])

    async def on_call_tool(ctx, params):
        name = params.name
        args = params.arguments or {}
        if name == "echo":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=args.get("text", ""))],
                is_error=False,
            )
        if name == "adder":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(args.get("a", 0) + args.get("b", 0)))],
                is_error=False,
            )
        if name == "delete_thing":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Deleted {args.get('id', '?')}")],
                is_error=False,
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )

    server = Server("test-mcp-server", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    app = server.streamable_http_app()

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server_instance = uvicorn.Server(config)

    task = asyncio.create_task(server_instance.serve())
    # Give the server a moment to start.
    await asyncio.sleep(0.5)

    yield f"http://127.0.0.1:{port}/mcp"

    server_instance.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError:
        task.cancel()


@pytest.mark.asyncio
async def test_integration_connect_and_call(mcp_test_server, mcp_registry):
    """End-to-end: connect to a real MCP server, discover tools, call one."""
    url = mcp_test_server
    session, seed = mcp_registry
    await seed(
        "test-server",
        name="Test Server",
        enabled=1,
        transport="streamable_http",
        url=url,
        options='{"allow_private_network": true}',
    )
    ctx = _make_context(tempfile.mkdtemp(), db_session=session)
    tools = await attach_mcp_tools(ctx, ["mcp"])

    # Should discover 3 tools.
    tool_names = sorted(tools.keys())
    assert "mcp__test_server__echo" in tool_names
    assert "mcp__test_server__adder" in tool_names
    assert "mcp__test_server__delete_thing" in tool_names

    # Call echo.
    echo_tool = tools["mcp__test_server__echo"]
    result = await echo_tool.execute({"text": "hello mcp"}, ctx)
    assert result["content"] == "hello mcp"

    # Call adder.
    adder_tool = tools["mcp__test_server__adder"]
    result = await adder_tool.execute({"a": 3, "b": 4}, ctx)
    assert result["content"] == "7"

    # Cleanup.
    await ctx.cleanup()


@pytest.mark.asyncio
async def test_integration_allowlist_denylist(mcp_test_server, mcp_registry):
    """Test allowlist/denylist filtering with a real server."""
    url = mcp_test_server
    session, seed = mcp_registry
    await seed(
        "filtered",
        name="Filtered",
        enabled=1,
        transport="streamable_http",
        url=url,
        options='{"allow_private_network": true, "tools": {"allowlist": ["echo", "adder"], "denylist": ["delete_*"]}}',
    )
    ctx = _make_context(tempfile.mkdtemp(), db_session=session)
    tools = await attach_mcp_tools(ctx, ["mcp"])

    assert "mcp__filtered__echo" in tools
    assert "mcp__filtered__adder" in tools
    assert "mcp__filtered__delete_thing" not in tools

    await ctx.cleanup()


@pytest.mark.asyncio
async def test_integration_specific_server(mcp_test_server, mcp_registry):
    """Test mcp:<slug> declaration targets only the specified server."""
    url = mcp_test_server
    session, seed = mcp_registry
    await seed(
        "target",
        name="Target",
        enabled=1,
        transport="streamable_http",
        url=url,
        options='{"allow_private_network": true}',
    )
    await seed(
        "other",
        name="Other",
        enabled=1,
        transport="streamable_http",
        url=url,
        options='{"allow_private_network": true}',
    )
    ctx = _make_context(tempfile.mkdtemp(), db_session=session)
    tools = await attach_mcp_tools(ctx, ["mcp:target"])
    # Only tools from "target" server.
    assert all(t.startswith("mcp__target__") for t in tools)
    assert len(tools) > 0


# ---------------------------------------------------------------------------
# _resolve_final_url — redirect probes strip sensitive headers cross-origin
# ---------------------------------------------------------------------------


class _FakeProbe:
    """One redirect probe client: returns the next canned response."""

    def __init__(self, responses: list):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_resolve_final_url_strips_sensitive_headers_cross_origin(monkeypatch):
    """A cross-origin redirect hop must lose EVERY sensitive header
    (Authorization, X-API-Key, ...) — not just httpx's built-in
    Authorization/Cookie pair — and the caller gets the stripped headers."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    responses = [
        MagicMock(status_code=302, headers={"location": "https://target.example.com/mcp"}),
        MagicMock(status_code=200, headers={}),
    ]

    def _fake_client(**kwargs):
        return _FakeProbe(responses)

    with patch("httpx2.AsyncClient", _fake_client):
        session = McpServerSession(
            "srv",
            {"url": "https://origin.example.com/mcp",
             "allowed_hosts": ["origin.example.com", "target.example.com"]},
            {"Authorization": "Bearer tok", "X-API-Key": "k", "X-Custom": "c"},
        )
        final_url, headers = await session._resolve_final_url(
            "https://origin.example.com/mcp"
        )

    assert final_url == "https://target.example.com/mcp"
    assert "Authorization" not in headers
    assert "X-API-Key" not in headers
    assert headers.get("X-Custom") == "c"


@pytest.mark.asyncio
async def test_resolve_final_url_keeps_headers_on_same_origin_redirect(monkeypatch):
    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    responses = [
        MagicMock(status_code=302, headers={"location": "https://origin.example.com/v2/mcp"}),
        MagicMock(status_code=200, headers={}),
    ]

    def _fake_client(**kwargs):
        return _FakeProbe(responses)

    with patch("httpx2.AsyncClient", _fake_client):
        session = McpServerSession(
            "srv",
            {"url": "https://origin.example.com/mcp",
             "allowed_hosts": ["origin.example.com"]},
            {"Authorization": "Bearer tok", "X-API-Key": "k"},
        )
        final_url, headers = await session._resolve_final_url(
            "https://origin.example.com/mcp"
        )

    assert final_url == "https://origin.example.com/v2/mcp"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-API-Key"] == "k"


@pytest.mark.asyncio
async def test_resolve_final_url_blocks_unvalidated_redirect_target(monkeypatch):
    """A redirect to a host outside allowed_hosts must fail closed."""
    monkeypatch.delenv("SSRF_ENABLED", raising=False)
    responses = [
        MagicMock(status_code=302, headers={"location": "https://evil.example.com/mcp"}),
    ]

    def _fake_client(**kwargs):
        return _FakeProbe(responses)

    with patch("httpx2.AsyncClient", _fake_client):
        session = McpServerSession(
            "srv",
            {"url": "https://origin.example.com/mcp",
             "allowed_hosts": ["origin.example.com"]},
            {"Authorization": "Bearer tok"},
        )
        with pytest.raises(McpConnectError):
            await session._resolve_final_url("https://origin.example.com/mcp")


# ---------------------------------------------------------------------------
# _connect_sse transport factory
# ---------------------------------------------------------------------------


async def test_connect_sse_factory_signature_and_redirect_policy():
    """The injected SSE factory must accept the SDK's keyword signature
    (headers/auth/timeout) — ssl_verify=False previously crashed with
    TypeError — and disable redirect following so credentials cannot ride an
    unvalidated 302 to a new origin."""
    sess = McpServerSession(
        "srv",
        {"connect_timeout_seconds": 5},
        {"Authorization": "Bearer tok"},
        ssl_verify=False,
    )

    async def _fake_resolve(url):
        return url, {"Authorization": "Bearer tok"}

    sess._resolve_final_url = _fake_resolve

    captured: dict = {}

    class _FakeSSE:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return AsyncMock(), AsyncMock()

        async def __aexit__(self, *args):
            return False

    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=MagicMock(tools=[_make_mcp_tool()]))

    with (
        patch("mcp.client.sse.sse_client", _FakeSSE),
        patch("mcp.ClientSession", return_value=fake_session),
    ):
        task = asyncio.create_task(sess._connect_sse("https://mcp.example.com/sse"))
        await asyncio.wait_for(sess._ready.wait(), timeout=5)
        sess._shutdown.set()
        await task

    factory = captured["httpx_client_factory"]
    # The SDK calls the factory with headers/auth/timeout keywords — the
    # regression: an argument-less factory raised TypeError there.
    client = factory(headers={"x-extra": "1"}, auth=None, timeout=None)
    try:
        assert client.follow_redirects is False
        # SDK-passed headers win when provided
        assert client.headers.get("x-extra") == "1"
        assert client.headers.get("Authorization") is None
    finally:
        import httpx2
        await client.aclose()
    # headers=None falls back to the resolved connect headers
    client = factory(headers=None, auth=None, timeout=None)
    try:
        assert client.headers.get("Authorization") == "Bearer tok"
    finally:
        await client.aclose()
    # The SDK's injected 5s connect timeout must NOT win — slow-handshake
    # servers would be cut at 5s. The factory always applies the explicit
    # MCP default (30s connect / 300s read), matching the streamable path.
    client = factory(headers=None, auth=None, timeout=httpx2.Timeout(5.0, read=300.0))
    try:
        assert client.timeout.connect == 30.0
        assert client.timeout.read == 300.0
    finally:
        await client.aclose()
