"""Generic third-party API request tool.

Allows agents to call customer-configured HTTP APIs using secret_ref for
authentication. Secrets are resolved server-side; plaintext never reaches the LLM.

When endpoint_key is provided, method default, path, per-environment base_url,
allowed_hosts, response_json_path, and auth defaults are resolved server-side
from the project's integrations/custom-api catalog; raw path + base_url remain
the fallback when no endpoint_key is given.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import Tool, ToolContext
from ._http import ensure_http_client
from .shell import redact_secrets

_log = logging.getLogger(__name__)

# Hard cap on the response body regardless of max_response_chars: a
# misbehaving upstream must not be able to make the agent process exhaust
# memory. JSON responses are parsed in full, so the cap rejects, not truncates.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024

_SENSITIVE_HEADER_RE = re.compile(
    r"authorization|cookie|x-api-key|api-key|token|secret|password|bearer",
    re.IGNORECASE,
)

_SENSITIVE_FIELD_RE = re.compile(
    r"token|secret|password|cookie|authorization|api_key|credential|private_key",
    re.IGNORECASE,
)

_SAFE_RESPONSE_HEADER_BLOCKLIST = frozenset({"set-cookie", "www-authenticate"})

_METADATA_IP = "169.254.169.254"


def _is_sensitive_header(name: str) -> bool:
    return bool(_SENSITIVE_HEADER_RE.search(name))


def _is_private_ip(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Alternate inet_aton forms ipaddress rejects but glibc's getaddrinfo
        # maps to real addresses ("2130706433" → 127.0.0.1) — re-check the
        # mapped quad so the always-on literal guard sees them too.
        from ._ssrf import _inet_aton_mapped

        mapped = _inet_aton_mapped(host)
        return _is_private_ip(mapped) if mapped is not None else False
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_private
        or str(addr) == _METADATA_IP
    )


def _validate_url_safety(url: str, allowed_hosts: list[str] | None) -> str | None:
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
        return (
            f"Host {host!r} is not in allowed_hosts {allowed_hosts}. "
            "Set allowed_hosts to permit this target."
        )

    import os
    allow_private = os.environ.get("THIRD_PARTY_API_ALLOW_PRIVATE", "").lower() in (
        "1", "true", "yes"
    )
    if not allow_private and _is_private_ip(host):
        return f"Target IP {host!r} is in a blocked range (loopback/private/link-local/multicast)"

    try:
        from ._ssrf import validate_url as ssrf_validate_url
        ssrf_validate_url(url, allow_any_port=True)
        # DNS-level check (SSRF_ENABLED gate): the literal IP/host checks
        # above never see a hostname that resolves to an internal address.
        # Run resolve_and_validate directly — validate_outbound_url would
        # re-apply the port allowlist and defeat the allow_any_port above
        # (api_request targets are user-configured endpoints like Kong:8001
        # or Ollama:11434; resolved IPs still get blocked.
        from ._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from ._ssrf import resolve_and_validate
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            resolve_and_validate(parsed.hostname, port)
    except Exception as exc:
        return f"SSRF check failed: {exc}"

    return None


def _redact_sensitive_fields(obj: Any, depth: int = 0, max_depth: int = 10) -> Any:
    if depth >= max_depth:
        return obj
    if isinstance(obj, dict):
        return {
            k: "[REDACTED]" if _SENSITIVE_FIELD_RE.search(str(k)) else _redact_sensitive_fields(v, depth + 1, max_depth)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_sensitive_fields(item, depth + 1, max_depth) for item in obj]
    return obj


def _redact_secret_values(obj: Any, secrets: list[str], depth: int = 0, max_depth: int = 10) -> Any:
    """Recursively scrub resolved secret VALUES from JSON response data.

    Field-name redaction (`_redact_sensitive_fields`) misses upstreams that
    echo credentials inside non-sensitive fields — `{"error": "Invalid API
    key sk-ant-xxx"}` leaks the key value into the LLM context. Every string
    leaf is passed through redact_secrets.
    """
    if depth >= max_depth:
        return obj
    if isinstance(obj, dict):
        return {k: _redact_secret_values(v, secrets, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_secret_values(item, secrets, depth + 1, max_depth) for item in obj]
    if isinstance(obj, str):
        return redact_secrets(obj, secrets)
    return obj


def _extract_json_path(data: Any, path: str) -> Any:
    parts = path.split(".")
    current = data
    for part in parts:
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


class ApiRequestTool(Tool):
    name = "api_request"
    prompt_hint = (
        "Call configured third-party HTTP APIs; auth is injected server-side via "
        "secret_ref, so never ask the user to paste a secret into chat. Use "
        "secret_vault list first to discover available keys."
    )
    description = (
        "Call a customer-configured third-party HTTP API. "
        "Authentication is handled server-side via secret_ref — "
        "plaintext secrets never appear in tool results. "
        "When endpoint_key is provided, method default, path, per-environment "
        "base_url, allowed_hosts, response_json_path, and auth defaults are "
        "resolved server-side from the project's integrations/custom-api catalog."
    )
    parameters = {
        "type": "object",
        "required": ["integration_key", "method"],
        "properties": {
            "integration_key": {
                "type": "string",
                "description": "Integration identifier from integrations/custom-api knowledge or project memory",
            },
            "endpoint_key": {
                "type": "string",
                "description": "Named endpoint from the project's integrations/custom-api catalog. Resolved server-side: the catalog supplies the method default, path, per-environment base_url, allowed_hosts, response_json_path, and auth defaults. Prefer endpoint_key over a raw path.",
            },
            "environment": {
                "type": "string",
                "description": "Target environment (dev/uat/int/prd)",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
            },
            "path": {
                "type": "string",
                "description": "Relative URL path (e.g. /api/v1/customers/{id}). Required unless endpoint_key is provided; must not be a full URL.",
            },
            "path_params": {
                "type": "object",
                "description": "Path parameter substitutions",
            },
            "query_params": {
                "type": "object",
                "description": "URL query parameters",
            },
            "headers": {
                "type": "object",
                "description": "Additional headers (auth headers are forbidden)",
            },
            "json_body": {
                "description": "JSON request body",
            },
            "auth_type": {
                "type": "string",
                "enum": ["bearer", "api_key_header", "basic", "cookie", "custom_header", "body_field", "none"],
                "default": "bearer",
            },
            "secret_ref": {
                "type": "string",
                "description": "Secret reference key (e.g. third_party:crm:uat). Resolved server-side. Used for single-secret auth.",
            },
            "secret_refs": {
                "type": "object",
                "description": "Multi-secret references for auth types needing multiple values (e.g. basic auth). Keys are placeholder names, values are secret_ref keys. e.g. {\"username\": \"svc:user\", \"password\": \"svc:pass\"}.",
            },
            "secret_scope": {
                "type": "string",
                "enum": ["project", "user"],
                "default": "project",
                "description": "Secret lookup scope: 'project' checks project secrets then user tokens; 'user' checks user tokens only",
            },
            "auth_header_name": {
                "type": "string",
                "description": "Header name for api_key_header/custom_header auth types",
            },
            "auth_prefix": {
                "type": "string",
                "description": "Value prefix for custom_header (e.g. 'Token '). Supports placeholders for multi-secret: '{username}:{password}'",
            },
            "auth_field_name": {
                "type": "string",
                "description": "JSON body field name for body_field auth type (e.g. 'apiKey')",
            },
            "response_mode": {
                "type": "string",
                "enum": ["json", "text", "status", "headers_only"],
                "default": "json",
            },
            "response_json_path": {
                "type": "string",
                "description": "JSONPath-like dot notation to extract response subset (e.g. 'data.items'); defaults to the catalog endpoint's response_json_path when endpoint_key is used",
            },
            "max_response_chars": {
                "type": "integer",
                "default": 20000,
                "maximum": 100000,
            },
            "base_url": {
                "type": "string",
                "description": "Override base URL (must match allowed_hosts). Normally resolved from integration config; overrides the catalog's per-environment base_url when endpoint_key is used",
            },
            "allowed_hosts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Allowed target hostnames for this request",
            },
            "timeout_seconds": {
                "type": "integer",
                "default": 30,
                "maximum": 120,
            },
            "require_confirmation": {
                "type": "boolean",
                "default": False,
                "description": "Force user confirmation before sending",
            },
        },
    }

    async def _apply_catalog_resolution(
        self,
        params: dict[str, Any],
        context: ToolContext,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Resolve endpoint_key against the project's integration catalog.

        Returns (effective_params, resolution_info). effective_params is a
        copy of params with catalog-supplied defaults filled in; when no
        endpoint_key is given, params is returned untouched (raw-path
        behavior preserved). On resolution failure an error dict is returned
        as the first element with resolution_info None. Never raises.
        """
        endpoint_key = params.get("endpoint_key")
        if not endpoint_key:
            return params, None

        integration_key = params["integration_key"]
        environment: str | None = params.get("environment")

        from ._catalog import (
            load_integration_catalog,
            resolve_catalog_endpoint,
            strip_jsonpath_prefix,
        )

        catalog = await load_integration_catalog(context)
        if not catalog:
            return (
                {
                    "error": (
                        "integration catalog not found at knowledge/integrations/custom-api.md. "
                        "Read it with knowledge_rw or onboard the system first "
                        "(see skill integration/custom-api-onboarding)."
                    ),
                    "integration_key": integration_key,
                    "endpoint_key": endpoint_key,
                },
                None,
            )

        resolved = resolve_catalog_endpoint(
            catalog, integration_key, endpoint_key, environment
        )
        if resolved is None:
            return (
                {
                    "error": (
                        f"Endpoint {endpoint_key!r} not found in catalog for "
                        f"integration {integration_key!r}. Check "
                        "knowledge/integrations/custom-api.md or onboard the "
                        "integration first (see skill "
                        "integration/custom-api-onboarding)."
                    ),
                    "integration_key": integration_key,
                    "endpoint_key": endpoint_key,
                },
                None,
            )

        endpoint = resolved["endpoint"]
        environments = resolved["environments"]
        auth = resolved["auth"]
        defaults = resolved["defaults"]

        if not endpoint.get("path"):
            return (
                {
                    "error": (
                        f"Catalog endpoint {endpoint_key!r} for integration "
                        f"{integration_key!r} has no path"
                    ),
                    "integration_key": integration_key,
                    "endpoint_key": endpoint_key,
                },
                None,
            )

        effective = dict(params)

        # Environment selection: explicit wins; otherwise the first catalog
        # environment (YAML order) is used and echoed to the LLM.
        if environment is None and environments:
            environment = next(iter(environments))
            effective["environment"] = environment

        env_entry = environments.get(environment) if environment else None
        if not isinstance(env_entry, dict):
            env_entry = None

        # method: catalog is a default only — an explicit agent method wins.
        effective.setdefault("method", str(endpoint.get("method", "GET")).upper())
        # path: the catalog is authoritative (it is the verified contract).
        effective["path"] = endpoint["path"]
        # base_url: explicit param wins; else the catalog env entry; the
        # existing env-var fallback below still applies when neither exists.
        if not effective.get("base_url") and env_entry and env_entry.get("base_url"):
            effective["base_url"] = env_entry["base_url"]
        # allowed_hosts: an explicit param replaces the catalog allowlist.
        if not effective.get("allowed_hosts") and env_entry and env_entry.get("allowed_hosts"):
            effective["allowed_hosts"] = env_entry["allowed_hosts"]
        # response_json_path: default from catalog, normalized to dot notation.
        if not effective.get("response_json_path") and endpoint.get("response_json_path"):
            effective["response_json_path"] = strip_jsonpath_prefix(
                str(endpoint["response_json_path"])
            )
        # Auth defaults from the catalog block. secret_ref may be a pattern
        # like "third_party:{ik}:{environment}"; substitution is lenient — a
        # missing stored key falls through to the tool's secure-prompt flow.
        if auth.get("type"):
            effective.setdefault("auth_type", auth["type"])
        if auth.get("secret_ref"):
            pattern = str(auth["secret_ref"])
            if environment:
                pattern = pattern.replace("{environment}", environment)
            effective.setdefault("secret_ref", pattern)
        if auth.get("header_name"):
            effective.setdefault("auth_header_name", auth["header_name"])
        # Per-integration defaults.
        if defaults.get("timeout_seconds"):
            effective.setdefault("timeout_seconds", defaults["timeout_seconds"])
        if defaults.get("max_response_chars"):
            effective.setdefault("max_response_chars", defaults["max_response_chars"])
        # side_effect: true forces the user-confirmation gate even for GET.
        # false never disables the tool's existing write/prod confirmation.
        if endpoint.get("side_effect") is True:
            effective["require_confirmation"] = True

        return effective, {
            "resolved_from_catalog": True,
            "endpoint_key": endpoint_key,
            "resolved_environment": environment,
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        integration_key: str = params["integration_key"]
        effective, catalog_info = await self._apply_catalog_resolution(params, context)
        if "error" in effective:
            return effective
        params = effective
        method: str = params["method"].upper()
        path: str = params.get("path", "")
        environment: str | None = params.get("environment")
        endpoint_key: str | None = params.get("endpoint_key")
        auth_type: str = params.get("auth_type", "bearer")
        secret_ref: str | None = params.get("secret_ref")
        response_mode: str = params.get("response_mode", "json")
        response_json_path: str | None = params.get("response_json_path")
        # LLM 常把数字参数传成字符串（"30000"）——str/int 混比会抛 TypeError。
        try:
            max_response_chars = int(params.get("max_response_chars", 20000))
        except (TypeError, ValueError):
            max_response_chars = 20000
        max_response_chars = min(max(max_response_chars, 1000), 100000)
        try:
            timeout_seconds = int(params.get("timeout_seconds", 30))
        except (TypeError, ValueError):
            timeout_seconds = 30
        timeout_seconds = min(max(timeout_seconds, 1), 120)
        require_confirmation: bool = params.get("require_confirmation", False)
        allowed_hosts: list[str] | None = params.get("allowed_hosts")
        path_params: dict[str, str] = params.get("path_params") or {}
        query_params: dict[str, Any] = params.get("query_params") or {}
        extra_headers: dict[str, str] = params.get("headers") or {}
        json_body = params.get("json_body")
        auth_header_name: str | None = params.get("auth_header_name")
        auth_prefix: str | None = params.get("auth_prefix")

        # 1. Validate extra headers do not contain sensitive keys
        for header_name in extra_headers:
            if _is_sensitive_header(header_name):
                return {
                    "error": (
                        f"Header {header_name!r} is forbidden. "
                        "Auth headers must be provided via secret_ref and auth_type."
                    ),
                    "forbidden_header": header_name,
                }

        # 2. Runtime guard: path is required unless endpoint_key supplied one
        if not path:
            return {
                "error": (
                    "path is required unless endpoint_key is provided "
                    "(then the path is resolved from the integration catalog)"
                ),
                "integration_key": integration_key,
            }

        # 3. Validate path is relative
        if path.lower().startswith(("http://", "https://")):
            return {
                "error": "path must be a relative URL path, not a full URL. Use base_url for the base.",
                "path": path,
            }

        # 4. Resolve base_url
        base_url: str | None = params.get("base_url")
        if not base_url:
            env_suffix = f"_{environment.upper()}" if environment else ""
            cfg_key = f"THIRD_PARTY_{integration_key.upper()}_BASE_URL{env_suffix}"
            base_url = context.cfg(cfg_key)
        if not base_url and environment:
            fallback_key = f"THIRD_PARTY_{integration_key.upper()}_BASE_URL"
            base_url = context.cfg(fallback_key)
        if not base_url:
            return {
                "error": (
                    f"No base URL configured for integration {integration_key!r}. "
                    f"Set THIRD_PARTY_{integration_key.upper()}_BASE_URL "
                    f"(or _BASE_URL_{(environment or 'ENV').upper()}) in integration settings, "
                    "or pass base_url parameter."
                ),
                "integration_key": integration_key,
            }

        # 5. Substitute path params and build full URL
        resolved_path = path
        for key, value in path_params.items():
            resolved_path = resolved_path.replace(f"{{{key}}}", str(value))
        try:
            resolved_path = resolved_path.format(**path_params)
        except (KeyError, IndexError, ValueError):
            pass

        full_url = base_url.rstrip("/") + "/" + resolved_path.lstrip("/")

        # 6. SSRF / allowlist validation
        safety_error = _validate_url_safety(full_url, allowed_hosts)
        if safety_error:
            return {"error": f"URL safety check failed: {safety_error}", "url": full_url}

        # 7. Write-operation or prod confirmation
        write_gate = require_confirmation or method in ("POST", "PUT", "PATCH", "DELETE")
        prod_gate = (environment or "").lower().startswith(("prd", "prod", "production"))
        # Per-agent opt-out: automation agents (QA data-setup, etc.) set
        # api_request_no_confirm so their run loop is not blocked on a prompt.
        # The production gate always stays — prod writes still confirm.
        if write_gate and getattr(context, "api_request_no_confirm", False):
            write_gate = False
        needs_confirmation = write_gate or prod_gate
        if needs_confirmation:
            session = getattr(context, "session", None)
            if session is None:
                # Fail closed like the MCP confirmation gate: a write/prod
                # request with nobody to ask must not silently proceed.
                return {
                    "error": "Confirmation required but no interactive session is available",
                    "cancelled": True,
                }
            prompt_id = str(uuid.uuid4())
            try:
                confirm_result = await session.request_user_selection(
                    prompt_id=prompt_id,
                    field_key=f"api_request_confirm_{prompt_id}",
                    question=f"Allow {method} {full_url}?",
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
                return {"error": "Confirmation prompt timed out", "cancelled": True}
            if confirm_result != "allow":
                return {"error": "Request denied by user", "cancelled": True}

        # 8. Secret resolution - supports single secret_ref or multi secret_refs
        secrets: dict[str, str] = {}  # placeholder -> plaintext value

        if auth_type != "none":
            secret_refs_map: dict[str, str] = {}
            if params.get("secret_refs"):
                secret_refs_map = dict(params["secret_refs"])
            elif secret_ref:
                secret_refs_map = {"_default": secret_ref}

            secret_scope = params.get("secret_scope", "project")
            save_to_user = secret_scope == "user"

            from ._auth import get_secret_optional, get_token_optional

            for placeholder, skey in secret_refs_map.items():
                if save_to_user:
                    val = await get_token_optional(context, skey)
                else:
                    val = await get_secret_optional(context, skey, environment=environment)
                if val is None:
                    session = getattr(context, "session", None)
                    if session:
                        prompt_id = str(uuid.uuid4())
                        try:
                            prompt_result = await session.request_user_selection(
                                prompt_id=prompt_id,
                                field_key=skey,
                                question=f"API token required for {integration_key} ({environment or 'default'}): {skey}",
                                kind="text",
                                secret=True,
                                task_id=context.current_task_id,
                                service_key=skey,
                                environment=environment,
                                save_to_user_tokens=save_to_user,
                                save_to_project_secrets=not save_to_user,
                                options=[],
                                timeout=300.0,
                            )
                        except RuntimeError:
                            return {
                                "error": f"Secret prompt for {skey!r} timed out",
                                "secret_ref": skey,
                            }
                        if prompt_result == "secret_saved":
                            if context.db_session:
                                await context.db_session.commit()
                            if save_to_user:
                                val = await get_token_optional(context, skey)
                            else:
                                val = await get_secret_optional(context, skey, environment=environment)
                    if not val:
                        return {
                            "error": f"Secret {skey!r} not available after prompt",
                            "secret_ref": skey,
                        }
                secrets[placeholder] = val

        # Single-secret convenience (for single secret_ref backward compat)
        _secret = secrets.get("_default")

        # 9. Build request headers with auth injection
        request_headers: dict[str, str] = dict(extra_headers)

        def _first_secret() -> str | None:
            """Return the single secret value for single-secret auth types."""
            if _secret:
                return _secret
            # No resolved "_default" — with exactly one stored value that's
            # it (backward compat); with multiple named secrets, picking "the
            # first" would send the wrong credential (e.g. a username as a
            # bearer token), so treat it as unresolved instead.
            values = list(secrets.values())
            return values[0] if len(values) == 1 else None

        if auth_type != "none" and secrets:
            first_secret = _first_secret()
            if first_secret is None and auth_type in ("bearer", "api_key_header", "cookie"):
                return {
                    "error": "No usable secret for single-secret auth — check secret_ref "
                    "(multiple named secrets resolved but no '_default')",
                }
            if auth_type == "bearer":
                request_headers["Authorization"] = f"Bearer {first_secret}"
            elif auth_type == "api_key_header":
                header = auth_header_name or "X-API-Key"
                request_headers[header] = first_secret
            elif auth_type == "basic":
                user = secrets.get("username", "")
                pwd = secrets.get("password", _secret or "")
                encoded = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                request_headers["Authorization"] = f"Basic {encoded}"
            elif auth_type == "cookie":
                request_headers["Cookie"] = first_secret
            elif auth_type == "custom_header":
                if not auth_header_name:
                    return {
                        "error": "auth_header_name is required when auth_type is 'custom_header'",
                    }
                if auth_prefix and "{" in auth_prefix:
                    # Multi-secret template: e.g. "{username}:{password}"
                    try:
                        request_headers[auth_header_name] = auth_prefix.format(**secrets)
                    except KeyError as exc:
                        return {
                            "error": f"auth_prefix placeholder {exc} not found in secret_refs",
                        }
                else:
                    if len(secrets) > 1:
                        # auth_prefix without a {} template silently dropped
                        # every secret but the first — fail loudly instead.
                        return {
                            "error": (
                                "auth_prefix has no {} placeholder but multiple "
                                "secrets are configured; use a template like "
                                "'{username}:{password}' to map all of them"
                            ),
                        }
                    request_headers[auth_header_name] = f"{auth_prefix or ''}{_first_secret()}"
            elif auth_type == "body_field":
                field_name = params.get("auth_field_name")
                if not field_name:
                    return {
                        "error": "auth_field_name is required when auth_type is 'body_field'",
                    }
                if json_body is None:
                    json_body = {}
                if len(secrets) > 1:
                    return {
                        "error": (
                            "auth_type 'body_field' supports a single secret; "
                            "configure one secret_ref for this field"
                        ),
                    }
                json_body[field_name] = _first_secret()

        # 10. Send request
        http = ensure_http_client(context, target_url=full_url)
        parsed_host = urlparse(full_url).hostname or full_url

        _log.warning(
            "api_request: %s %s | integration=%r env=%r endpoint=%r status=pending",
            method, f"{parsed_host}{resolved_path}", integration_key, environment,
            endpoint_key,
        )

        t0 = time.monotonic()
        try:
            request_kwargs: dict[str, Any] = {
                "headers": request_headers,
                "timeout": float(timeout_seconds),
            }
            if query_params:
                request_kwargs["params"] = query_params
            if json_body is not None and method not in ("GET", "HEAD"):
                request_kwargs["json"] = json_body

            # Stream the body with a hard byte cap — a misbehaving upstream
            # must not be able to exhaust agent memory with an unbounded
            # response (max_response_chars only bounds what reaches the LLM).
            async with http.stream(method, full_url, **request_kwargs) as response:
                response_status = response.status_code
                response_headers = dict(response.headers)
                content_type = response_headers.get("content-type", "")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE_BYTES:
                        return {
                            "error": f"Response exceeds the {_MAX_RESPONSE_BYTES // (1024 * 1024)}MB limit",
                            "integration_key": integration_key,
                            "url": full_url,
                        }
                    chunks.append(chunk)
                response_text = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )
        except httpx.TimeoutException:
            _log.warning(
                "api_request timeout: %s %s | integration=%r env=%r endpoint=%r",
                method, parsed_host, integration_key, environment, endpoint_key,
            )
            return {"error": f"Request timed out after {timeout_seconds}s", "integration_key": integration_key}
        except httpx.RequestError as exc:
            _log.warning(
                "api_request network error: %s %s | integration=%r env=%r endpoint=%r | %s",
                method, parsed_host, integration_key, environment, endpoint_key, exc,
            )
            return {"error": f"Network error: {exc}", "integration_key": integration_key}

        duration_ms = int((time.monotonic() - t0) * 1000)
        response_size = len(response_text)

        _log.warning(
            "api_request: %s %s | integration=%r env=%r endpoint=%r status=%d duration_ms=%d response_bytes=%d",
            method, f"{parsed_host}{resolved_path}",
            integration_key, environment, endpoint_key,
            response_status, duration_ms, response_size,
        )

        # 11. Process response
        result: dict[str, Any] = {
            "status": response_status,
            "content_type": content_type,
            "truncated": False,
        }
        if catalog_info:
            result["catalog"] = catalog_info
        if secret_ref:
            result["secret_ref"] = secret_ref

        if response_mode == "status":
            result["ok"] = 200 <= response_status < 300
            return result

        if response_mode == "headers_only":
            # Header VALUES can echo secrets too — echo-anything gateways
            # (httpbin-style debug endpoints, error pages echoing the
            # authorization header) would otherwise leak them to the LLM,
            # same as the text/json body scrub below.
            result["headers"] = {
                k: redact_secrets(v, secrets)
                for k, v in response_headers.items()
                if k.lower() not in _SAFE_RESPONSE_HEADER_BLOCKLIST
            }
            return result

        if response_mode == "text":
            truncated = len(response_text) > max_response_chars
            # Upstreams echo credentials back in error bodies ("Invalid API
            # key: sk-...", "401 bearer <token>") — scrub resolved secret
            # values before the body reaches the LLM / history.
            result["body"] = redact_secrets(response_text, secrets)[:max_response_chars]
            result["truncated"] = truncated
            return result

        # response_mode == "json" (default)
        parsed_body: Any
        if "json" in content_type:
            try:
                parsed_body = json.loads(response_text)
            except Exception:
                truncated = len(response_text) > max_response_chars
                result["body"] = redact_secrets(response_text, secrets)[:max_response_chars]
                result["truncated"] = truncated
                return result
        else:
            parsed_body = response_text

        if response_json_path and isinstance(parsed_body, (dict, list)):
            extracted = _extract_json_path(parsed_body, response_json_path)
            if extracted is not None:
                parsed_body = extracted

        parsed_body = _redact_sensitive_fields(parsed_body)
        # Field-name redaction can't see secrets echoed in non-sensitive
        # fields — scrub values too before the body reaches the LLM.
        parsed_body = _redact_secret_values(parsed_body, secrets)

        if isinstance(parsed_body, str):
            truncated = len(parsed_body) > max_response_chars
            result["body"] = redact_secrets(parsed_body, secrets)[:max_response_chars]
            result["truncated"] = truncated
        else:
            import json as _json
            serialized = _json.dumps(parsed_body, ensure_ascii=False)
            truncated = len(serialized) > max_response_chars
            if truncated:
                result["body"] = serialized[:max_response_chars]
                result["truncated"] = True
            else:
                result["body"] = parsed_body
                result["truncated"] = False

        return result
