"""Per-run loopback RPC bridge: scripts call agent tools in-process.

A script is a sandboxed subprocess (sitecustomize audit hooks, blocked bash
commands) and cannot reach the service process's tools directly. When a
script declares tools, ``execute_script`` starts one ToolBridge for the run:
a 127.0.0.1 TCP server speaking one-line JSON (NDJSON), guarded by a
per-run PIN handed to the child through env. The server builds a headless
ToolContext for the run's owner (``triggered_by``) and dispatches calls to
the declared allowlist — anything not declared answers ``tool_not_allowed``.

Headless means ``interactive=False`` and ``session=None``: confirmation
gates fail closed with a readable error instead of blocking on a human
nobody is going to ask (session.py raises UserSelectionUnavailableError
immediately when interactive is off; gate code paths with no session return
their deny answer).

The bridge lives only as long as the run: started before spawn, closed in
``execute_script``'s finally, never listening on anything but loopback.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import uuid
from typing import Any, Callable

from ..tools.base import ToolContext

_log = logging.getLogger("agents_universe.scripts.bridge")

PROTOCOL_V = 1
# The client waits slightly longer than the server's per-tool timeout so a
# tool_timeout response (rather than a client-side socket error) is what the
# script sees when a tool hangs.
TOOL_TIMEOUT = 120.0
CLIENT_TIMEOUT = TOOL_TIMEOUT + 10.0

ENV_BRIDGE = "AGENT_TOOL_BRIDGE"
ENV_PIN = "AGENT_TOOL_BRIDGE_PIN"

# Error codes on the wire (error.code in a failed response).
CODE_AUTH_FAILED = "auth_failed"
CODE_TOOL_NOT_ALLOWED = "tool_not_allowed"
CODE_TOOL_ERROR = "tool_error"
CODE_TOOL_TIMEOUT = "tool_timeout"
CODE_BAD_REQUEST = "bad_request"


def _error(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message[:500]}


class ToolBridge:
    """One bridge instance per script run; owns the server and the run's ToolContext."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        pin: str,
        registry: dict[str, Any],
        context: ToolContext,
        run_id: str,
    ) -> None:
        self._server: asyncio.AbstractServer | None = None
        self.host = host
        self.port = port
        self.pin = pin
        self._registry = registry
        self._context = context
        self._run_id = run_id
        # Tools share one ToolContext (browser, http client, mcp manager are
        # expensive); serialize calls so a context's mutable fields (notably
        # db_session) never see two coroutines at once.
        self._lock = asyncio.Lock()
        self._handlers: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────

    @classmethod
    async def start(
        cls,
        *,
        project_id: str,
        project_fs_path: str,
        user_id: str,
        run_id: str,
        declared_tools: list[str],
        db_session_factory: Callable[[], Any],
        secret_key: str,
    ) -> "ToolBridge":
        from ..tools.registry import build_tool_registry
        from .declarations import filter_runtime_tools

        tools = filter_runtime_tools(list(declared_tools))
        static_names = [t for t in tools if t != "mcp" and not t.startswith("mcp:")]
        registry = build_tool_registry(static_names)
        # build_tool_registry force-injects plan_task even for an explicit
        # allowlist; scripts cannot use it (deny list) — drop it again.
        registry.pop("plan_task", None)

        context = ToolContext(
            project_id=project_id,
            project_fs_path=project_fs_path,
            conversation_id=f"script-run:{run_id}",
            user_id=user_id,
            secret_key=secret_key,
            db_session_factory=db_session_factory,
        )
        # Headless: no conversation, no human to answer confirmation gates.
        context.interactive = False
        context.session = None
        context.api_request_no_confirm = False

        await _apply_integration_settings(context, db_session_factory)

        if any(t == "mcp" or t.startswith("mcp:") for t in tools):
            await _attach_mcp(context, tools, project_id, project_fs_path, registry)

        pin = secrets.token_urlsafe(32)
        # The instance exists before the server does so the accept handler can
        # close over it directly; the port is unknown until start_server runs,
        # so it is filled in afterwards (the child learns the port via env,
        # which is only read after start() returns).
        bridge = cls(
            host="127.0.0.1",
            port=0,
            pin=pin,
            registry=registry,
            context=context,
            run_id=run_id,
        )

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                bridge._handlers.add(task)
            try:
                await bridge._serve(reader, writer)
            finally:
                if task is not None:
                    bridge._handlers.discard(task)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 - peer may vanish first
                    pass

        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        bridge._server = server
        bridge.port = server.sockets[0].getsockname()[1]
        _log.info(
            "tool bridge up for script run %s on 127.0.0.1:%d (%d tools)",
            run_id, bridge.port, len(registry),
        )
        return bridge

    def env_vars(self) -> dict[str, str]:
        """Env handed to the child process. PIN never appears in any log."""
        return {
            ENV_BRIDGE: f"{self.host}:{self.port}",
            ENV_PIN: self.pin,
        }

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._context.cleanup()

    # ── wire protocol ────────────────────────────────────────────────────

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                line = await reader.readline()
            except (asyncio.CancelledError, ConnectionError, OSError):
                return
            if not line:
                return
            resp = await self._dispatch(line)
            try:
                writer.write(json.dumps(resp, ensure_ascii=False, default=str).encode("utf-8") + b"\n")
                await writer.drain()
            except (ConnectionError, OSError):
                return
            if resp.get("ok") is False and resp.get("error", {}).get("code") == CODE_AUTH_FAILED:
                return

    async def _dispatch(self, line: bytes) -> dict[str, Any]:
        req_id: str | None = None
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            req_id = req.get("id") if isinstance(req.get("id"), str) else None
        except (ValueError, UnicodeDecodeError):
            return {"v": PROTOCOL_V, "id": req_id, "ok": False,
                    "error": _error(CODE_BAD_REQUEST, "malformed request line")}

        def fail(code: str, message: str) -> dict[str, Any]:
            return {"v": PROTOCOL_V, "id": req_id, "ok": False, "error": _error(code, message)}

        pin = req.get("pin")
        if not isinstance(pin, str) or not hmac.compare_digest(pin, self.pin):
            _log.warning("tool bridge auth failure for run %s", self._run_id)
            return fail(CODE_AUTH_FAILED, "invalid bridge credentials")

        tool_name = req.get("tool")
        params = req.get("params", {})
        if not isinstance(tool_name, str) or not tool_name:
            return fail(CODE_BAD_REQUEST, "missing tool name")
        if not isinstance(params, dict):
            return fail(CODE_BAD_REQUEST, "params must be an object")

        tool = self._registry.get(tool_name)
        if tool is None:
            # Fail-closed: not in the run's allowlist (or deny-listed).
            _log.info(
                "tool bridge refused %r for run %s (not declared)", tool_name, self._run_id
            )
            return fail(CODE_TOOL_NOT_ALLOWED, f"tool {tool_name!r} is not declared for this script")

        try:
            async with self._lock:
                # Short-lived session per call: the owner's identity for
                # get_secret/get_token lookups; released immediately after.
                async with self._context.db_session_factory() as db:
                    self._context.db_session = db
                    try:
                        result = await asyncio.wait_for(
                            tool.execute(params, self._context), timeout=TOOL_TIMEOUT
                        )
                    finally:
                        self._context.db_session = None
        except asyncio.TimeoutError:
            _log.warning("tool %r timed out for run %s", tool_name, self._run_id)
            return fail(CODE_TOOL_TIMEOUT, f"tool {tool_name!r} timed out after {TOOL_TIMEOUT:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the tool's own failure surface
            _log.warning("tool %r failed for run %s: %s", tool_name, self._run_id, exc)
            return fail(CODE_TOOL_ERROR, str(exc))

        if not isinstance(result, dict):
            result = {"value": result}
        return {"v": PROTOCOL_V, "id": req_id, "ok": True, "result": result}


# ── context wiring shared with the API layer ─────────────────────────────


async def _apply_integration_settings(
    context: ToolContext, db_session_factory: Callable[[], Any]
) -> None:
    """Best-effort integration settings (same dict agent_turn builds).

    Degrades to system defaults when running outside the API package
    (agent-core standalone tests): no settings module, no base_url overrides.
    """
    try:
        from api.config import get_settings
    except ImportError:
        return
    from sqlalchemy import text as sa_text

    s = get_settings()
    context.ssl_verify = s.llm_ssl_verify
    context.browser_ssl_verify = s.browser_ssl_verify
    context.integration_settings = {
        "ATLASSIAN_BASE_URL":   s.atlassian_base_url,
        "ATLASSIAN_AUTH_TYPE":  s.atlassian_auth_type,
        "JIRA_BASE_PATH":       s.atlassian_jira_base_path,
        "CONFLUENCE_BASE_PATH": s.atlassian_confluence_base_path,
        "GIT_BASE_URL":         s.git_base_url,
        "GIT_API_BASE_PATH":    s.git_api_base_path,
        "HTTPS_PROXY":          s.https_proxy,
        "NO_PROXY":             ",".join(filter(None, [s.no_proxy, s.app_no_proxy])),
        "SANDBOX_NETWORK":      s.sandbox_network,
    }
    # Per-user base_url overrides stored on user_tokens (Settings ->
    # Integrations) — same mapping agent_turn applies.
    service_to_setting = {
        "git":         "GIT_BASE_URL",
        "jira":        "ATLASSIAN_BASE_URL",
        "confluence":  "CONFLUENCE_BASE_URL",
    }
    try:
        async with db_session_factory() as db:
            result = await db.execute(
                sa_text(
                    "SELECT service_key, base_url FROM user_tokens "
                    "WHERE user_id = :uid AND base_url IS NOT NULL"
                ),
                {"uid": context.user_id},
            )
            for row in result:
                key = service_to_setting.get(row[0])
                if key and row[1]:
                    context.integration_settings[key] = row[1]
    except Exception as exc:  # noqa: BLE001 - defaults are fine without overrides
        _log.debug("integration base_url override load failed: %s", exc)


async def _attach_mcp(
    context: ToolContext,
    tools: list[str],
    project_id: str,
    project_fs_path: str,
    registry: dict[str, Any],
) -> None:
    """Sync the project's MCP catalog (when the API layer is present) and attach."""
    from pathlib import Path

    if project_fs_path:
        try:
            from api.services.mcp_sync import sync_mcp_servers_from_file

            async with context.db_session_factory() as db:
                await sync_mcp_servers_from_file(
                    db,
                    project_id,
                    Path(project_fs_path) / "knowledge" / "integrations" / "mcp-servers.md",
                )
        except Exception as exc:  # noqa: BLE001 - stale catalog still works
            _log.warning("MCP catalog sync failed for run %s: %s", project_id, exc)
    try:
        from ..tools.mcp_client import attach_mcp_tools

        mcp_tools = await attach_mcp_tools(context, tools)
        registry.update(mcp_tools)
    except Exception as exc:  # noqa: BLE001 - MCP never blocks the run
        _log.warning("MCP tool attachment failed: %s", exc)
