"""MCP (Model Context Protocol) client integration.

Connects to external MCP servers declared in the project's
``integrations/mcp-servers`` knowledge file, discovers their tools, and
exposes them to the agent as dynamic ``Tool`` instances.

Tool naming: ``mcp__<server_slug>__<tool_name>`` -- the prefix avoids
collisions with built-in tools and lets the UI render a distinct badge.

Transport: Streamable HTTP (default, with SSE fallback).  stdio is reserved
for a future iteration.

Secrets are resolved via the existing project_secrets / user_tokens vault
(``_auth.get_secret``) and injected into transport headers -- plaintext never
reaches the LLM, logs, or tool output.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Byte caps for MCP image content blocks : ~7.5MB decoded.
_MAX_IMAGE_BASE64 = 10_000_000
_MAX_IMAGE_BYTES = 7_000_000
# text content blocks had no cap — a chatty/malicious MCP server
# could dump tens of MB straight into the LLM context. Mirrors the byte caps
# on images and on api_request/web_fetch responses.
_MAX_TEXT_CHARS = 1_000_000

# MIME type → media-filename extension. Splitting "image/svg+xml" on "/"
# would yield "svg+xml", whose '+' fails the media filename whitelist and
# the saved image 404s forever; unknown types fall back to png.
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}

from .base import Tool, ToolContext
from ._mcp_catalog import load_mcp_servers, sanitize_slug

_log = logging.getLogger(__name__)

# Reuse security helpers from api_request (self-contained, no httpx dep).
from .api_request import _is_sensitive_header, _is_private_ip, _redact_sensitive_fields  # noqa: E402

_MAX_TOOL_NAME_LEN = 64
_MAX_DESCRIPTION_LEN = 1024
_MAX_TOOLS_PER_SERVER = 64
_CALL_TIMEOUT_HARD_CAP = 300


# ---------------------------------------------------------------------------
# URL safety (per-server allow_private_network)
# ---------------------------------------------------------------------------


def _validate_mcp_url(url: str, allowed_hosts: list[str] | None, allow_private: bool) -> str | None:
    """Return an error message if the URL is unsafe, or None if OK."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return f"Failed to parse URL: {exc}"

    if parsed.scheme not in ("http", "https"):
        return f"Scheme must be http or https, got: {parsed.scheme!r}"

    host = parsed.hostname
    if not host:
        return "URL has no hostname"

    if allowed_hosts and host.lower() not in [h.lower() for h in allowed_hosts]:
        return f"Host {host!r} is not in allowed_hosts {allowed_hosts}"

    if not allow_private:
        if _is_private_ip(host):
            return f"Target IP {host!r} is in a blocked range (loopback/private/link-local/multicast)"
        try:
            from ._ssrf import validate_url as ssrf_validate_url
            ssrf_validate_url(url, allow_any_port=True)
            # DNS-level check (SSRF_ENABLED gate): a hostname resolving to an
            # internal address is invisible to the literal checks above.
            from ._http import validate_outbound_url
            validate_outbound_url(url)
        except Exception as exc:
            return f"SSRF check failed: {exc}"

    return None


# ---------------------------------------------------------------------------
# Header / secret resolution
# ---------------------------------------------------------------------------


async def _resolve_auth_headers(context: ToolContext, cfg: dict[str, Any]) -> dict[str, str]:
    """Build HTTP headers for the MCP server connection.

    Resolves the secret via project_secrets / user_tokens and renders it into
    the configured header.  Plaintext is never logged.
    """
    headers: dict[str, str] = {}

    # Non-sensitive extra headers from config.
    for name, value in (cfg.get("headers") or {}).items():
        if _is_sensitive_header(name):
            raise ValueError(
                f"Sensitive header {name!r} in MCP server config - use auth.secret_ref instead"
            )
        headers[str(name)] = str(value)

    auth = cfg.get("auth") or {}
    auth_type = (auth.get("type") or "none").lower()
    if auth_type == "none":
        return headers

    secret_ref = auth.get("secret_ref")
    if not secret_ref:
        raise ValueError(f"auth.type={auth_type!r} but no secret_ref configured")

    secret_scope = (auth.get("secret_scope") or "project").lower()
    from ._auth import get_secret, get_token

    if secret_scope == "user":
        secret_value = await get_token(context, secret_ref)
    else:
        secret_value = await get_secret(context, secret_ref)

    if auth_type == "bearer":
        header_name = "Authorization"
        template = auth.get("value_template") or "Bearer {secret}"
    elif auth_type == "header":
        header_name = auth.get("header_name") or "X-API-Key"
        template = auth.get("value_template") or "{secret}"
    else:
        raise ValueError(f"Unsupported auth type: {auth_type!r}")

    headers[header_name] = template.format(secret=secret_value)
    return headers


# ---------------------------------------------------------------------------
# McpServerSession - single server connection lifecycle
# ---------------------------------------------------------------------------


class McpConnectError(Exception):
    """Raised when an MCP server connection fails."""


class McpServerSession:
    """Manages the lifecycle of a single MCP server connection.

    The mcp SDK's transport factories are async context managers, so the
    connection is held alive inside a background task.  ``start()`` spawns the
    task and waits for the session to become ready (or timeout).  ``close()``
    signals shutdown and joins the task.
    """

    def __init__(self, slug: str, cfg: dict[str, Any], headers: dict[str, str], ssl_verify: bool = True):
        self.slug = slug
        self.cfg = cfg
        self._headers = headers
        self._ssl_verify = ssl_verify
        self._session = None          # mcp.ClientSession
        self._tools: list = []        # cached list_tools result
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._connect_error: Exception | None = None
        self._transport_used: str = ""

    @property
    def transport(self) -> str:
        return self._transport_used

    async def start(self) -> None:
        """Spawn the background connection task and wait for readiness."""
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run())
        timeout = float(self.cfg.get("connect_timeout_seconds", 10))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            await self._cancel()
            raise McpConnectError(f"MCP server {self.slug!r}: connect timeout ({timeout}s)")
        if self._connect_error:
            raise McpConnectError(f"MCP server {self.slug!r}: {self._connect_error}") from self._connect_error

    async def _run(self) -> None:
        """Background task: enter transport + session context managers, stay alive."""
        try:
            await self._run_with_transport()
        except Exception as exc:
            self._connect_error = exc
            self._session = None  # invalidate stale session on connection drop
            self._ready.set()
        except asyncio.CancelledError:
            raise

    async def _run_with_transport(self) -> None:
        transport = (self.cfg.get("transport") or "auto").lower()
        url = self.cfg["url"]

        if transport == "stdio":
            raise NotImplementedError(
                "stdio transport is not supported yet; use streamable_http or sse"
            )

        if transport in ("auto", "streamable_http"):
            try:
                await self._connect_streamable_http(url)
                return
            except Exception as exc:
                if transport == "streamable_http":
                    raise
                _log.warning(
                    "MCP server %r: streamable_http failed (%s), falling back to SSE",
                    self.slug, exc,
                )

        if transport in ("auto", "sse"):
            await self._connect_sse(url)
            return

        raise ValueError(f"Unknown MCP transport: {transport!r}")

    async def _resolve_final_url(self, url: str) -> tuple[str, dict[str, str]]:
        """Follow the redirect chain manually, validating every hop .

        follow_redirects=True let httpx follow redirects with NO re-validation
        of the target (a compromised MCP server could 302 to an internal
        address and escape the SSRF/private-IP checks) and forward custom auth
        headers (X-API-Key) to arbitrary hosts. Each hop is validated with the
        same policy as the original URL; cross-origin hops drop EVERY
        sensitive header (mirroring httpx's own Authorization/Cookie
        stripping, but covering all injected credential headers like
        X-API-Key). Returns (final_url, headers) — the caller must connect
        with the returned headers so a cross-origin final hop never receives
        the configured credentials.
        """
        import httpx2
        from urllib.parse import urljoin, urlsplit
        from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT

        allowed_hosts = [h.lower() for h in (self.cfg.get("allowed_hosts") or [])]
        allow_private = bool(self.cfg.get("allow_private_network", False))
        current = url
        headers = dict(self._headers)
        for _ in range(5):
            try:
                async with httpx2.AsyncClient(
                    headers=headers,
                    follow_redirects=False,
                    timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT),
                    verify=self._ssl_verify,
                ) as probe:
                    resp = await probe.get(current)
            except Exception as exc:
                # Probe failures (405 on GET-only endpoints, TLS, etc.) must
                # not break the real connect — return the last validated URL
                # and let the SDK surface the actual error. Any redirects we
                # did follow were validated; anything we didn't follow will
                # fail closed because the real client uses follow_redirects=False.
                _log.debug("MCP redirect probe for %r failed: %s", current, exc)
                return current, headers
            if resp.status_code not in (301, 302, 303, 307, 308):
                return current, headers
            location = resp.headers.get("location")
            if not location:
                return current, headers
            next_url = urljoin(current, location)
            error = _validate_mcp_url(next_url, allowed_hosts, allow_private)
            if error:
                raise McpConnectError(
                    f"MCP server {self.slug!r}: redirect to {next_url!r} blocked ({error})"
                )
            if urlsplit(next_url).netloc.lower() != urlsplit(current).netloc.lower():
                # Strip ALL sensitive headers before the next hop is probed —
                # httpx's built-in stripping covers only Authorization/Cookie,
                # while auth here can also ride in X-API-Key and friends.
                headers = {
                    k: v for k, v in headers.items() if not _is_sensitive_header(k)
                }
            current = next_url
        _log.warning("MCP server %r: redirect chain exceeded 5 hops, connecting to last URL", self.slug)
        return current, headers

    async def _connect_streamable_http(self, url: str) -> None:
        import httpx2
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT

        self._transport_used = "streamable_http"
        # pre-follow the redirect chain with per-hop validation, then
        # connect with follow_redirects=False so any un-validated 3xx fails.
        # _resolve_final_url also returns the headers to use: cross-origin
        # hops strip the sensitive ones so credentials never leave the
        # configured origin.
        url, connect_headers = await self._resolve_final_url(url)
        async with httpx2.AsyncClient(
            headers=connect_headers,
            follow_redirects=False,
            timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            verify=self._ssl_verify,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._tools = (await session.list_tools()).tools
                    self._ready.set()
                    await self._shutdown.wait()

    async def _connect_sse(self, url: str) -> None:
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        import httpx2
        from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT

        self._transport_used = "sse"
        # same redirect policy as streamable_http — pre-validate the
        # chain, then connect without auto-following (the SDK's default
        # client follows redirects and would forward credentials to any
        # new origin). Use the returned (cross-origin-stripped) headers.
        url, connect_headers = await self._resolve_final_url(url)
        # sse_client calls the factory with headers/auth/timeout keywords —
        # accept (and honor) all three. verify/ssl_verify applies on every
        # path, not only when verification is disabled.
        def _factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            # SDK 总会塞进自己的 5s 连接超时——慢握手服务器会被掐断，一律用
            # 显式超时（与 streamable_http 路径一致）。
            return httpx2.AsyncClient(
                headers=headers or connect_headers,
                follow_redirects=False,
                timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
                auth=auth,
                verify=self._ssl_verify,
            )

        async with sse_client(url, headers=connect_headers, httpx_client_factory=_factory) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._tools = (await session.list_tools()).tools
                self._ready.set()
                await self._shutdown.wait()

    @property
    def tools(self) -> list:
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        """Call a tool on the MCP server with a timeout.

        The SDK's ``call_tool`` takes ``read_timeout_seconds`` natively; pass it
        so the request is timed out at the protocol layer (no orphaned in-flight
        request ids from hard-cancelling the coroutine).  ``wait_for`` remains
        only as a backstop with a small margin.
        """
        if self._session is None:
            raise McpConnectError(f"MCP server {self.slug!r}: session not active")
        timeout = min(
            float(self.cfg.get("call_timeout_seconds", 60)),
            _CALL_TIMEOUT_HARD_CAP,
        )
        return await asyncio.wait_for(
            self._session.call_tool(name, arguments, read_timeout_seconds=timeout),
            timeout=timeout + 10,
        )

    async def close(self) -> None:
        """Signal shutdown and wait for the background task to finish."""
        self._shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _cancel(self) -> None:
        self._shutdown.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


# ---------------------------------------------------------------------------
# McpConnectionManager - per-ToolContext, lazy discovery
# ---------------------------------------------------------------------------


class McpConnectionManager:
    """Manages all MCP server connections for a single ToolContext.

    Mirrors the lazy-init pattern of ``ToolContext.ensure_browser``:
    connections are established on first use and cleaned up via ``close_all()``.
    """

    def __init__(self, context: ToolContext):
        self._context = context
        self._sessions: dict[str, McpServerSession] = {}

    async def discover_tools(
        self,
        servers: dict[str, dict[str, Any]],
        declared_slugs: set[str] | None = None,
    ) -> dict[str, Tool]:
        """Connect to declared servers and return ``{tool_name: McpProxyTool}``.

        ``declared_slugs`` of None means "all enabled servers".  A single
        server failure produces a warning but does not block others.
        """
        tool_name_re = re.compile(r"^[a-zA-Z0-9_]+$")

        # Filter to declared servers.
        if declared_slugs is not None:
            servers = {s: c for s, c in servers.items() if s in declared_slugs}

        if not servers:
            return {}

        # Connect to all servers concurrently.
        async def _connect_one(slug: str, cfg: dict) -> tuple[str, McpServerSession | None]:
            try:
                headers = await _resolve_auth_headers(self._context, cfg)
            except Exception as exc:
                _log.warning("MCP server %r: header resolution failed: %s", slug, exc)
                return slug, None

            url = cfg.get("url", "")
            allowed_hosts = cfg.get("allowed_hosts")
            allow_private = bool(cfg.get("allow_private_network", False))
            url_error = _validate_mcp_url(url, allowed_hosts, allow_private)
            if url_error:
                _log.warning("MCP server %r: URL safety check failed: %s", slug, url_error)
                return slug, None

            session = McpServerSession(slug, cfg, headers, self._context.ssl_verify)
            try:
                await session.start()
            except Exception as exc:
                # Catch everything: McpConnectError (connect failures) as well as
                # config errors like a non-numeric connect_timeout_seconds.  One
                # bad server must not abort discovery for the others.
                _log.warning("MCP server %r: connection failed: %s", slug, exc)
                return slug, None
            return slug, session

        results = await asyncio.gather(
            *[_connect_one(s, c) for s, c in servers.items()],
            return_exceptions=True,
        )

        tools: dict[str, Tool] = {}
        for entry in results:
            if isinstance(entry, BaseException):
                _log.warning("MCP server discovery error: %s", entry)
                continue
            slug, session = entry
            if session is None:
                continue
            self._sessions[slug] = session

            # Build proxy tools from discovered tools.
            server_cfg = servers[slug]
            allowlist = server_cfg.get("tools", {}).get("allowlist") or []
            denylist = server_cfg.get("tools", {}).get("denylist") or []
            server_name = server_cfg.get("name", slug)

            count = 0
            for mcp_tool in session.tools:
                orig_name = getattr(mcp_tool, "name", "")
                if not orig_name:
                    continue

                # Apply allow/deny filters.
                if denylist and _matches_any(orig_name, denylist):
                    continue
                if allowlist and not _matches_any(orig_name, allowlist):
                    continue

                # Sanitize tool name component.
                tool_comp = re.sub(r"[^a-zA-Z0-9_]", "_", orig_name)
                if not tool_name_re.match(tool_comp):
                    tool_comp = "tool"
                full_name = f"mcp__{slug}__{tool_comp}"[:_MAX_TOOL_NAME_LEN]

                if full_name in tools:
                    _log.debug("MCP tool name collision, skipping: %s", full_name)
                    continue

                tools[full_name] = McpProxyTool(
                    name=full_name,
                    server_slug=slug,
                    server_name=server_name,
                    orig_tool_name=orig_name,
                    mcp_tool=mcp_tool,
                    server_cfg=server_cfg,
                    manager=self,
                )
                count += 1
                if count >= _MAX_TOOLS_PER_SERVER:
                    _log.warning(
                        "MCP server %r: reached max_tools_per_server (%d), truncating",
                        slug, _MAX_TOOLS_PER_SERVER,
                    )
                    break

        return tools

    async def get_session(self, slug: str) -> McpServerSession | None:
        return self._sessions.get(slug)

    async def close_all(self) -> None:
        """Disconnect all MCP server sessions."""
        if not self._sessions:
            return
        await asyncio.gather(
            *[s.close() for s in self._sessions.values()],
            return_exceptions=True,
        )
        self._sessions.clear()


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Check if a name matches any glob pattern in the list."""
    return any(fnmatch.fnmatch(name, p) for p in patterns)


# ---------------------------------------------------------------------------
# McpProxyTool - dynamic Tool that proxies to an MCP server tool
# ---------------------------------------------------------------------------


class McpProxyTool(Tool):
    """A Tool instance backed by a remote MCP server tool.

    Created dynamically by ``McpConnectionManager.discover_tools`` -- never
    instantiated directly by the registry.
    """

    def __init__(
        self,
        name: str,
        server_slug: str,
        server_name: str,
        orig_tool_name: str,
        mcp_tool: Any,
        server_cfg: dict[str, Any],
        manager: McpConnectionManager,
    ):
        self._name = name
        self._server_slug = server_slug
        self._server_name = server_name
        self._orig_tool_name = orig_tool_name
        self._mcp_tool = mcp_tool
        self._server_cfg = server_cfg
        self._manager = manager

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        desc = getattr(self._mcp_tool, "description", "") or ""
        label = f"[MCP:{self._server_name}] "
        return (label + desc)[:_MAX_DESCRIPTION_LEN]

    @property
    def parameters(self) -> dict:
        schema = getattr(self._mcp_tool, "input_schema", None)
        if not isinstance(schema, dict):
            return {"type": "object", "properties": {}}
        return schema

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Execute the MCP tool and convert the result to a tool-result dict."""
        # 1. Confirmation gate.
        if await self._needs_confirmation(params, context):
            allowed = await self._prompt_confirmation(params, context)
            if not allowed:
                return {"error": "User declined to execute MCP tool", "cancelled": True}

        # 2. Call the MCP server tool.
        session = await self._manager.get_session(self._server_slug)
        if session is None:
            return {"error": f"MCP server {self._server_slug!r} is not connected"}

        try:
            result = await session.call_tool(self._orig_tool_name, params or None)
        except TimeoutError:
            return {"error": f"MCP tool call timed out"}
        except Exception as exc:
            _log.warning("MCP tool %s failed: %s", self._name, exc, exc_info=True)
            return {"error": str(exc)[:500]}

        # 3. Convert result content.
        return self._convert_result(result, context)

    # -- confirmation -------------------------------------------------------

    async def _needs_confirmation(self, params: dict[str, Any], context: ToolContext) -> bool:
        """Check if this tool call requires user confirmation."""
        tools_cfg = self._server_cfg.get("tools") or {}

        # Explicit require_confirmation patterns.
        require_patterns = tools_cfg.get("require_confirmation") or []
        if _matches_any(self._orig_tool_name, require_patterns):
            return True

        # Auto-confirm for destructive tools.
        if self._server_cfg.get("require_confirmation_for_write", True):
            annotations = getattr(self._mcp_tool, "annotations", None)
            if annotations and getattr(annotations, "destructive_hint", None) is True:
                return True

        return False

    async def _prompt_confirmation(self, params: dict[str, Any], context: ToolContext) -> bool:
        """Ask the user to allow/deny the tool call. Returns True if allowed."""
        session = getattr(context, "session", None)
        if session is None:
            # Fail closed: with no interactive session there is nobody to ask,
            # and a destructive MCP call must not proceed on a silent default.
            return False

        prompt_id = str(uuid.uuid4())
        # Build a short summary of params for the prompt.
        param_summary = str(params)[:200]
        question = f"Allow MCP tool {self._name}?"
        if param_summary and param_summary != "{}":
            question += f"\nArguments: {param_summary}"
        try:
            result = await session.request_user_selection(
                prompt_id=prompt_id,
                field_key=f"mcp_confirm_{prompt_id}",
                question=question,
                kind="selection",
                options=[
                    {"label": "Allow", "value": "allow"},
                    {"label": "Deny", "value": "deny"},
                ],
                allow_other=False,
                task_id=context.current_task_id,
                timeout=120.0,
            )
        except RuntimeError:
            return False
        return result == "allow"

    # -- result conversion --------------------------------------------------

    def _convert_result(self, result: Any, context: ToolContext) -> dict[str, Any]:
        """Convert MCP CallToolResult to a tool-result dict."""
        is_error = getattr(result, "is_error", False)
        content_blocks = getattr(result, "content", []) or []

        text_parts: list[str] = []
        images: list[dict] = []
        text_chars = 0

        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                # cap accumulated text (see _MAX_TEXT_CHARS).
                part = getattr(block, "text", "")
                remaining = _MAX_TEXT_CHARS - text_chars
                if remaining <= 0:
                    continue
                if len(part) > remaining:
                    text_parts.append(part[:remaining] + "\n…[truncated: MCP text content too large]")
                    text_chars = _MAX_TEXT_CHARS
                else:
                    text_parts.append(part)
                    text_chars += len(part)
            elif block_type == "image":
                img = self._save_image(block, context)
                if img:
                    images.append(img)

        output: dict[str, Any] = {"content": "\n".join(text_parts) if text_parts else ""}

        if images:
            output["images"] = images

        if is_error:
            output["error"] = output["content"] or "MCP tool returned an error"

        # Redact sensitive fields.
        if self._server_cfg.get("redact_response", True):
            output = _redact_sensitive_fields(output)

        return output

    def _save_image(self, block: Any, context: ToolContext) -> dict | None:
        """Save an MCP ImageContent block to the media dir and return the image descriptor."""
        try:
            data = getattr(block, "data", "")
            mime_type = getattr(block, "mime_type", "image/png")
            # Explicit MIME → extension map: splitting "image/svg+xml" on "/"
            # yields "svg+xml", whose '+' fails the media filename whitelist
            # and the saved image 404s forever. Unknown types fall back to png.
            ext = _MIME_EXT.get(mime_type.split(";")[0].strip().lower(), "png")

            # Byte-level cap BEFORE decoding: a huge or malicious image block
            # must not exhaust memory or project disk (mirrors the 5MB cap in
            # api_request.
            if len(data) > _MAX_IMAGE_BASE64:
                _log.warning(
                    "MCP image from %s too large: %d base64 chars, skipping",
                    self._server_name, len(data),
                )
                return None

            media_dir = Path(context.conversation_media_dir)
            media_dir.mkdir(parents=True, exist_ok=True)
            filename = f"mcp_{uuid.uuid4().hex[:8]}.{ext}"
            output_path = media_dir / filename

            raw = base64.b64decode(data, validate=True)
            if len(raw) > _MAX_IMAGE_BYTES:
                _log.warning(
                    "MCP image from %s too large: %d bytes, skipping",
                    self._server_name, len(raw),
                )
                return None
            output_path.write_bytes(raw)

            url = f"/api/media/{context.project_id}/{context.conversation_id}/{filename}"
            return {
                "id": filename,
                "url": url,
                "alt": f"MCP image from {self._server_name}",
                "path": str(output_path),
                "annotations": [],
            }
        except Exception as exc:
            _log.warning("MCP image save failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# attach_mcp_tools - entry point called from handlers.py
# ---------------------------------------------------------------------------


async def attach_mcp_tools(
    context: ToolContext,
    declared_tools: list[str],
) -> dict[str, Tool]:
    """Resolve ``mcp`` / ``mcp:<slug>`` markers and return discovered tools.

    Called from the WebSocket handler after ``Agent`` construction but before
    ``agent.run()``.  Never raises -- MCP failures are logged as warnings and
    the caller continues with whatever tools were successfully attached.
    """
    # Parse declarations.
    all_servers = False
    specific_slugs: set[str] = set()

    for entry in declared_tools:
        if entry == "mcp":
            all_servers = True
        elif entry.startswith("mcp:"):
            raw_slug = entry[4:]
            specific_slugs.add(sanitize_slug(raw_slug))

    if not all_servers and not specific_slugs:
        return {}

    # Load catalog.
    try:
        servers = await load_mcp_servers(context)
    except Exception as exc:
        _log.warning("MCP catalog load failed: %s", exc)
        return {}

    if not servers:
        return {}

    # Determine target servers.
    if all_servers:
        target_slugs = None  # all enabled servers
    else:
        target_slugs = specific_slugs
        unknown = target_slugs - set(servers.keys())
        if unknown:
            _log.warning("MCP servers declared but not in catalog: %s", sorted(unknown))

    # Create connection manager on the context if not yet present.
    if context.mcp_manager is None:
        context.mcp_manager = McpConnectionManager(context)

    # Discover and return tools.
    try:
        return await context.mcp_manager.discover_tools(servers, target_slugs)
    except Exception as exc:
        _log.warning("MCP tool discovery failed: %s", exc)
        return {}
