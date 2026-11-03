"""ToolBridge protocol tests: PIN gate, allowlist, timeouts, client/CLI roundtrip.

The bridge is the only channel through which a sandboxed script reaches the
service process's tools, so every failure mode a script can hit (wrong PIN,
undeclared tool, tool crash, tool hang, bridge down) is pinned here — plus
the leak surface: the PIN and results must only ever be where the protocol
puts them.

agent-core tests run without the ``api`` package: ToolBridge.start degrades
to system-default integration settings, which is exactly the standalone
path these tests exercise.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid

import pytest

from agent_core.scripts import agent_script_tools, tool_bridge
from agent_core.scripts.declarations import SCRIPT_TOOL_DENY, filter_runtime_tools, validate_declarations
from agent_core.scripts.tool_bridge import ToolBridge
from agent_core.tools.base import Tool, ToolContext


class _EchoTool(Tool):
    name = "echo"
    description = "Echo the params back"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, params: dict, context: ToolContext) -> dict:
        return {"echoed": params, "user_id": context.user_id}


class _BoomTool(Tool):
    name = "boom"
    description = "Always raises"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, params: dict, context: ToolContext) -> dict:
        raise RuntimeError("kaboom")


class _SlowTool(Tool):
    name = "slow"
    description = "Sleeps past the tool timeout"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, params: dict, context: ToolContext) -> dict:
        await asyncio.sleep(5)
        return {"done": True}


class _FakeSession:
    """Async CM stand-in for an AsyncSession — the bridge only opens it."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _factory() -> _FakeSession:
    return _FakeSession()


async def _start_bridge(tmp_path, tools: list[str], user_id: str = "owner-1") -> ToolBridge:
    bridge = await ToolBridge.start(
        project_id="proj-1",
        project_fs_path=str(tmp_path),
        user_id=user_id,
        run_id=str(uuid.uuid4()),
        declared_tools=tools,
        db_session_factory=_factory,
        secret_key="",
    )
    bridge._registry["echo"] = _EchoTool()
    bridge._registry["boom"] = _BoomTool()
    bridge._registry["slow"] = _SlowTool()
    return bridge


def _raw_request(host: str, port: int, payload: dict, timeout: float = 5.0) -> dict:
    """Send one NDJSON request over a fresh socket and parse the reply line."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    assert buf, "bridge closed without a reply"
    return json.loads(buf.decode())


async def _call(name: str, params: dict | None = None) -> dict:
    """call_tool is synchronous by design (scripts are sync processes); in
    these tests the server shares this event loop, so the client must not
    block it — run the socket I/O in a worker thread."""
    return await asyncio.to_thread(agent_script_tools.call_tool, name, params)


async def _cli(argv: list[str]) -> int:
    return await asyncio.to_thread(agent_script_tools.main, argv)


async def _raw(host: str, port: int, payload: dict, timeout: float = 5.0) -> dict:
    return await asyncio.to_thread(_raw_request, host, port, payload, timeout)


def _forged_pin_then_eof(host: str, port: int, good_pin: str) -> dict:
    """Send a forged PIN followed by the real one on ONE connection: the
    server must answer auth_failed and drop the socket without ever running
    the second request. Returns the parsed first reply after confirming EOF."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(5)
        forged = json.dumps(
            {"v": 1, "id": "x", "pin": "not-the-pin", "tool": "echo", "params": {}}
        )
        real = json.dumps(
            {"v": 1, "id": "y", "pin": good_pin, "tool": "echo", "params": {}}
        )
        sock.sendall((forged + "\n" + real + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        resp = json.loads(buf.decode())
        assert sock.recv(65536) == b"", "server must close after auth_failed"
        return resp


@pytest.mark.asyncio
async def test_python_client_roundtrip(tmp_path, monkeypatch):
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)
        result = await _call("echo", {"msg": "hi"})
        assert result["echoed"] == {"msg": "hi"}
        # The owner's identity is what the ToolContext carries into the tool.
        assert result["user_id"] == "owner-1"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_undeclared_tool_is_refused(tmp_path, monkeypatch):
    # "shell" is not declared — and even if a row tried, the deny list keeps
    # it out of the registry (double gate, see declarations.SCRIPT_TOOL_DENY).
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)
        assert "shell" not in bridge._registry
        with pytest.raises(agent_script_tools.ToolBridgeError) as exc:
            await _call("shell", {"command": "ls"})
        assert exc.value.code == "tool_not_allowed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_wrong_pin_is_refused_and_connection_closes(tmp_path):
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        resp = await asyncio.to_thread(
            _forged_pin_then_eof, bridge.host, bridge.port, bridge.pin
        )
        assert resp["ok"] is False
        assert resp["error"]["code"] == "auth_failed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_tool_exception_maps_to_tool_error(tmp_path, monkeypatch):
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)
        with pytest.raises(agent_script_tools.ToolBridgeError) as exc:
            await _call("boom", {})
        assert exc.value.code == "tool_error"
        assert "kaboom" in str(exc.value)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_hanging_tool_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_bridge, "TOOL_TIMEOUT", 0.3)
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)
        with pytest.raises(agent_script_tools.ToolBridgeError) as exc:
            await _call("slow", {})
        assert exc.value.code == "tool_timeout"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_plan_task_is_stripped_even_when_declared(tmp_path, monkeypatch):
    # build_tool_registry force-injects plan_task for any allowlist; the
    # bridge must still refuse it (deny list + explicit pop).
    bridge = await _start_bridge(tmp_path, ["plan_task", "filesystem"])
    try:
        assert "plan_task" not in bridge._registry
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)
        with pytest.raises(agent_script_tools.ToolBridgeError) as exc:
            await _call("plan_task", {})
        assert exc.value.code == "tool_not_allowed"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_concurrent_bridges_get_unique_endpoints(tmp_path):
    a = await _start_bridge(tmp_path, ["filesystem"])
    b = await _start_bridge(tmp_path, ["filesystem"])
    try:
        assert (a.host, a.port) != (b.host, b.port)
        assert a.pin != b.pin
        # Each bridge answers only for its own PIN.
        resp = await _raw(a.host, a.port, {
            "v": 1, "id": "1", "pin": b.pin, "tool": "echo", "params": {},
        })
        assert resp["error"]["code"] == "auth_failed"
    finally:
        await a.close()
        await b.close()


def test_missing_bridge_env_is_unavailable(monkeypatch):
    monkeypatch.delenv(tool_bridge.ENV_BRIDGE, raising=False)
    monkeypatch.delenv(tool_bridge.ENV_PIN, raising=False)
    with pytest.raises(agent_script_tools.ToolBridgeError) as exc:
        agent_script_tools.call_tool("echo", {})
    assert exc.value.code == "unavailable"


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    # No bridge: exit 2 (bridge unusable), JSON error on stdout.
    monkeypatch.delenv(tool_bridge.ENV_BRIDGE, raising=False)
    monkeypatch.delenv(tool_bridge.ENV_PIN, raising=False)
    assert agent_script_tools.main(["call", "echo", "{}"]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False and out["error"]["code"] == "unavailable"

    # Bad usage: exit 2 as well (the CLI itself could not run the call).
    assert agent_script_tools.main([]) == 2

    # Malformed params JSON: exit 1 (request-level failure, bridge fine).
    assert agent_script_tools.main(["call", "echo", "{not json"]) == 1


@pytest.mark.asyncio
async def test_cli_success_and_tool_failure_exit_codes(tmp_path, monkeypatch, capsys):
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        monkeypatch.setenv(tool_bridge.ENV_BRIDGE, f"{bridge.host}:{bridge.port}")
        monkeypatch.setenv(tool_bridge.ENV_PIN, bridge.pin)

        assert await _cli(["call", "echo", json.dumps({"a": 1})]) == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["ok"] is True and out["result"]["echoed"] == {"a": 1}

        # Undeclared tool: the bridge answered (so not a bridge failure) → 1.
        assert await _cli(["call", "shell", "{}"]) == 1
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["error"]["code"] == "tool_not_allowed"
    finally:
        await bridge.close()


def test_deny_list_and_validation():
    # Save-time gate: every deny-listed tool is rejected by name.
    for name in sorted(SCRIPT_TOOL_DENY):
        with pytest.raises(ValueError, match=name):
            validate_declarations([], [name])
    # Runtime gate: legacy rows degrade to a smaller allowlist, never raise.
    stripped = filter_runtime_tools(["plan_task", "filesystem", "shell"])
    assert stripped == ["filesystem"]
    # Unknown tool names are rejected at save time too.
    with pytest.raises(ValueError, match="unknown tool"):
        validate_declarations([], ["definitely_not_a_tool"])


@pytest.mark.asyncio
async def test_headless_context_has_no_session(tmp_path):
    """Confirmation gates read context.interactive / context.session — the
    bridge must construct the context so they fail closed, never block."""
    bridge = await _start_bridge(tmp_path, ["filesystem"])
    try:
        ctx = bridge._context
        assert ctx.interactive is False
        assert ctx.session is None
        assert ctx.api_request_no_confirm is False
        assert ctx.user_id == "owner-1"
        assert ctx.project_id == "proj-1"
    finally:
        await bridge.close()
