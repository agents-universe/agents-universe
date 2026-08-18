"""Integration catalog loader — resolves api_request endpoint_key to concrete
request defaults from the project's integrations/custom-api knowledge file.

The catalog is Markdown with one YAML fenced block per integration (see
knowledge/_template/custom-api.md). This module is the single server-side
reader so the catalog acts as the source of truth for endpoint resolution;
agents pass an endpoint_key and the tool fills in method default, path,
per-environment base_url, allowed_hosts, response_json_path, and auth
defaults from the catalog.

Parsing is deliberately permissive: a malformed block never breaks the whole
catalog, and a missing/unreadable file yields an empty dict (callers decide
fallback semantics).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from .base import ToolContext

_log = logging.getLogger(__name__)

CATALOG_SLUG = "integrations/custom-api"

_YAML_FENCE_RE = re.compile(
    r"^```ya?ml\s*\n(.*?)```", re.DOTALL | re.MULTILINE | re.IGNORECASE
)


def strip_jsonpath_prefix(path: str) -> str:
    """Normalize a catalog response_json_path to plain dot notation.

    The catalog template historically uses JSONPath-ish "$.data"; the tool's
    extractor expects "data". Leading "$." / "." characters are stripped.
    """
    return path.lstrip("$.")


async def load_integration_catalog(context: ToolContext) -> dict[str, dict[str, Any]]:
    """Load and index the project's integration catalog.

    Returns {integration_key: parsed_yaml_block}. Missing file, read errors,
    and per-block parse failures are logged and skipped — this function never
    raises and never returns None.
    """
    catalog_path = Path(context.knowledge_dir()) / "integrations" / "custom-api.md"
    if not await asyncio.to_thread(catalog_path.exists):
        _log.debug("integration catalog not found at %s", catalog_path)
        return {}

    try:
        content = await asyncio.to_thread(catalog_path.read_text, "utf-8")
    except OSError as exc:
        _log.warning("integration catalog read failed: %s", exc)
        return {}

    # Strip YAML frontmatter (the file may or may not have one).
    try:
        body = frontmatter.loads(content).content
    except Exception as exc:
        _log.warning("integration catalog frontmatter parse failed, using raw: %s", exc)
        body = content

    catalog: dict[str, dict[str, Any]] = {}
    for match in _YAML_FENCE_RE.finditer(body):
        try:
            block = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            _log.warning("integration catalog YAML block skipped: %s", exc)
            continue
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("integrations"), dict):
            # Wrapper form: {integrations: {key: {...}, ...}}
            for key, entry in block["integrations"].items():
                if isinstance(entry, dict):
                    catalog[str(key)] = entry
        elif isinstance(block.get("integrations"), list):
            # Wrapper form: {integrations: [{integration_key: ..., ...}, ...]}
            for entry in block["integrations"]:
                if isinstance(entry, dict) and entry.get("integration_key"):
                    catalog[str(entry["integration_key"])] = entry
        elif block.get("integration_key"):
            catalog[str(block["integration_key"])] = block
        else:
            _log.debug(
                "integration catalog block without integration_key skipped"
            )
    return catalog


def resolve_catalog_endpoint(
    catalog: dict[str, dict[str, Any]],
    integration_key: str,
    endpoint_key: str,
    environment: str | None,
) -> dict[str, Any] | None:
    """Look up an endpoint in the indexed catalog.

    Returns a merged view {endpoint, environments, auth, defaults, env_name}
    or None when the integration or endpoint is absent. Field-level
    resolution (environment selection, precedence) is left to the caller.
    """
    config = catalog.get(integration_key)
    if not config or not isinstance(config, dict):
        return None
    endpoints = config.get("endpoints") or {}
    endpoint = endpoints.get(endpoint_key)
    if not isinstance(endpoint, dict):
        return None
    return {
        "endpoint": endpoint,
        "environments": config.get("environments") or {},
        "auth": config.get("auth") or {},
        "defaults": config.get("defaults") or {},
        "env_name": environment,
    }
