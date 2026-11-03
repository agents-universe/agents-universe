"""Validation for a script's declared secrets and tools.

Both the REST layer (create/update endpoints) and the script_writer tool
validate through here so an invalid declaration is rejected at save time
with a readable message naming only the offending key/tool — never a value.

The declarations are what a run is authorized to do: secrets are decrypted
from the triggering user's key vault and injected as AGENT_SECRET_* env vars,
tools become the per-run tool bridge's allowlist. Anything not declared is
unavailable at runtime.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Mirrors how service_keys are written in the vault (user_tokens.service_key /
# project_secrets.service_key): identifier-like, no spaces, no dashes — the
# value becomes part of an env var name after uppercasing.
SERVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,100}$")

MAX_SECRETS = 10
MAX_TOOLS = 20

# Tools a script may never declare, enforced at save time AND stripped again
# at bridge startup (two gates — a row that predates the deny list must not
# become executable). Rationale per group:
# - plan_task / user_confirm / delegate_agent / list_agents: depend on the
#   LLM conversation loop or an interactive session a script does not have;
# - script_writer / scheduler: a script that can create scripts or tasks is
#   an escalation ring (it could re-declare wider privileges for its next run);
# - code_executor / shell / browser_playwright: nested execution surfaces
#   that would bypass the very sandbox this runner provides.
SCRIPT_TOOL_DENY: frozenset[str] = frozenset({
    "plan_task",
    "user_confirm",
    "delegate_agent",
    "list_agents",
    "script_writer",
    "scheduler",
    "secret_vault",
    "code_executor",
    "shell",
    "browser_playwright",
})

# MCP tools are dynamic (mcp = all enabled servers, mcp:<slug> = one server),
# so they cannot be checked against known_static_tool_names().
_MCP_ALL = "mcp"
_MCP_PREFIX = "mcp:"


def parse_secrets_json(text: str | None) -> list[str]:
    """Parse a stored secrets_json column; null/blank yields []."""
    return _parse_json_list(text, "secrets")


def parse_tools_json(text: str | None) -> list[str]:
    """Parse a stored tools_json column; null/blank yields []."""
    return _parse_json_list(text, "tools")


def _parse_json_list(text: str | None, label: str) -> list[str]:
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"Stored {label} declaration is not valid JSON") from None
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"Stored {label} declaration must be a JSON array of strings")
    return data


def serialize(items: list[str]) -> str | None:
    """Column value for a declaration list; empty becomes NULL."""
    return json.dumps(items, ensure_ascii=False) if items else None


def validate_declarations(secrets: list[Any], tools: list[Any]) -> tuple[list[str], list[str]]:
    """Validate and normalize both declaration lists.

    Returns the cleaned (secrets, tools) tuples. Raises ValueError with a
    message that names only keys/tool names — safe to surface verbatim.
    """
    clean_secrets = _validate_secrets(secrets)
    clean_tools = _validate_tools(tools)
    return clean_secrets, clean_tools


def _validate_secrets(secrets: list[Any]) -> list[str]:
    if not isinstance(secrets, list):
        raise ValueError("secrets must be a list of key-vault service_key names")
    if len(secrets) > MAX_SECRETS:
        raise ValueError(f"at most {MAX_SECRETS} secrets may be declared")
    seen: set[str] = set()
    clean: list[str] = []
    for item in secrets:
        if not isinstance(item, str) or not SERVICE_KEY_RE.match(item):
            raise ValueError(
                "secret names must be 1-100 chars of letters, digits and underscores"
            )
        # Case-insensitive dedup: the injected env var uppercases the key, so
        # "GitHub_Token" and "github_token" would collide at runtime.
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        clean.append(item)
    return clean


def _validate_tools(tools: list[Any]) -> list[str]:
    if not isinstance(tools, list):
        raise ValueError("tools must be a list of tool names")
    if len(tools) > MAX_TOOLS:
        raise ValueError(f"at most {MAX_TOOLS} tools may be declared")
    # known_static_tool_names is import-light (no optional module imports).
    from ..tools.registry import known_static_tool_names

    known = known_static_tool_names()
    seen: set[str] = set()
    clean: list[str] = []
    for item in tools:
        if not isinstance(item, str) or not item:
            raise ValueError("tool names must be non-empty strings")
        if item in SCRIPT_TOOL_DENY:
            raise ValueError(f"tool {item!r} is not available to scripts")
        is_mcp = item == _MCP_ALL or item.startswith(_MCP_PREFIX)
        if not is_mcp and item not in known:
            raise ValueError(f"unknown tool {item!r}")
        if item in seen:
            continue
        seen.add(item)
        clean.append(item)
    return clean


def filter_runtime_tools(tools: list[str]) -> list[str]:
    """Runtime counterpart of validate_declarations for the bridge.

    Drops deny-listed entries instead of raising: a row written before the
    deny list existed must degrade to a smaller allowlist, not fail the run.
    """
    return [t for t in tools if t not in SCRIPT_TOOL_DENY]
