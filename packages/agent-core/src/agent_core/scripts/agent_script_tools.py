"""Client for the per-run tool bridge — copied next to each script at run time.

Python scripts import this module directly (``from agent_script_tools import
call_tool``); the script's own directory is ``sys.path[0]``, so the copy
runner writes beside ``main.py`` resolves with zero configuration. Bash
scripts reach the same code through the generated ``agent-tool`` CLI, a thin
shell wrapper that execs ``tool_call.py`` (this module's ``main``).

Stdlib only — the file executes inside the sandboxed script process, where
third-party imports are not guaranteed and subprocess is blocked (strict
sitecustomize mode).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from typing import Any

# Set by ToolBridge.env_vars() — present only when the run declared tools.
ENV_BRIDGE = "AGENT_TOOL_BRIDGE"
ENV_PIN = "AGENT_TOOL_BRIDGE_PIN"

# Must outlast the server-side per-tool timeout so the bridge's own
# tool_timeout response (not a client socket error) is what surfaces.
CLIENT_TIMEOUT = 130.0

# Exit codes for the CLI form (tool_call.py):
#   0 — tool returned a result (even a semantic {"error": ...} one)
#   1 — the bridge answered with a failure (tool error / not declared / bad request)
#   2 — the bridge itself is unusable (absent, unreachable, timeout, bad PIN)
EXIT_OK = 0
EXIT_TOOL_FAILURE = 1
EXIT_BRIDGE_FAILURE = 2


class ToolBridgeError(RuntimeError):
    """The bridge refused or could not complete a call.

    ``code`` is the wire error code (auth_failed / tool_not_allowed /
    tool_error / tool_timeout / bad_request / unavailable / protocol).
    Tools that return ``{"error": ...}`` as a *result* do not raise — that is
    the tool's own answer, not a bridge failure.
    """

    def __init__(self, message: str, code: str = "unavailable") -> None:
        super().__init__(message)
        self.code = code


def _endpoint() -> tuple[str, int, str]:
    addr = os.environ.get(ENV_BRIDGE, "")
    pin = os.environ.get(ENV_PIN, "")
    if not addr or not pin:
        raise ToolBridgeError(
            "tool bridge is not available in this run (no tools declared?)",
            "unavailable",
        )
    host, _, port_s = addr.rpartition(":")
    if not host or not port_s.isdigit():
        raise ToolBridgeError(f"malformed {ENV_BRIDGE} value", "unavailable")
    return host, int(port_s), pin


def call_tool(name: str, params: dict[str, Any] | None = None, *, timeout: float = CLIENT_TIMEOUT) -> dict[str, Any]:
    """Call a declared tool and return its result dict.

    Raises ToolBridgeError when the bridge itself fails (tool not declared,
    tool raised, timeout, auth). A result containing an "error" key is a
    normal return — the tool answered, nothing broke on the wire.
    """
    host, port, pin = _endpoint()
    request = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "pin": pin,
        "tool": name,
        "params": params or {},
    }
    payload = (json.dumps(request, ensure_ascii=False, default=str) + "\n").encode("utf-8")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (ConnectionError, OSError, socket.timeout) as exc:
        raise ToolBridgeError(f"tool bridge unreachable: {exc}", "unavailable") from exc

    if not buf:
        raise ToolBridgeError("tool bridge closed the connection without a reply", "protocol")
    try:
        resp = json.loads(buf.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolBridgeError(f"malformed bridge reply: {exc}", "protocol") from exc
    if not isinstance(resp, dict):
        raise ToolBridgeError("malformed bridge reply", "protocol")

    if resp.get("ok") is True:
        result = resp.get("result")
        return result if isinstance(result, dict) else {"value": result}

    err = resp.get("error")
    code = err.get("code", "protocol") if isinstance(err, dict) else "protocol"
    message = err.get("message", "bridge call failed") if isinstance(err, dict) else "bridge call failed"
    raise ToolBridgeError(str(message), str(code))


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``agent-tool call <tool> [params-json]`` (stdin if no JSON arg)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "call":
        sys.stdout.write(
            json.dumps(
                {"ok": False,
                 "error": {"code": "bad_request",
                           "message": "usage: agent-tool call <tool> [params-json] (JSON on stdin when omitted)"}},
                ensure_ascii=False,
            ) + "\n"
        )
        return EXIT_BRIDGE_FAILURE
    tool = argv[1]
    try:
        if len(argv) >= 3:
            params = json.loads(argv[2])
        else:
            params = json.load(sys.stdin)
        if not isinstance(params, dict):
            raise ValueError("params must be a JSON object")
    except (ValueError, TypeError) as exc:
        sys.stdout.write(
            json.dumps({"ok": False, "error": {"code": "bad_request", "message": f"bad params: {exc}"}},
                       ensure_ascii=False) + "\n"
        )
        return EXIT_TOOL_FAILURE

    try:
        result = call_tool(tool, params)
    except ToolBridgeError as exc:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                ensure_ascii=False,
                default=str,
            ) + "\n"
        )
        if exc.code in ("auth_failed", "unavailable", "timeout", "tool_timeout", "protocol"):
            return EXIT_BRIDGE_FAILURE
        return EXIT_TOOL_FAILURE
    sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
