"""Kong API gateway tool — make authenticated HTTP requests through Kong."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qs, urljoin

import httpx

from .base import Tool, ToolContext
from ._auth import ToolAuthError, get_secret, get_secret_optional
from ._http import ensure_http_client
from .shell import redact_secrets

_log = logging.getLogger(__name__)

_MAX_RESPONSE = 50_000

def _parse_env_block(content: str) -> dict[str, str]:
    """Extract KEY=VALUE pairs from ```env code blocks in markdown."""
    result: dict[str, str] = {}
    in_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped in ("```env", "```dotenv"):
            in_block = True
            continue
        if in_block and stripped == "```":
            in_block = False
            continue
        if not in_block:
            continue
        if not stripped or stripped.startswith("#"):
            continue
        eq = stripped.find("=")
        if eq > 0:
            key = stripped[:eq].strip()
            value = stripped[eq + 1:].strip()
            if value:
                result[key] = value
    return result


def _resolve_kong_base_from_knowledge(context: ToolContext, env: str) -> str | None:
    """Resolve Kong base URL from project environment knowledge config block."""
    pc = context.project_context
    if pc is None:
        return None
    content = pc.loaded_content.get("environment/environment") or ""
    if not content:
        for slug, text in pc.loaded_content.items():
            if slug.endswith("/environment") or slug == "environment":
                content = text
                break
    if not content:
        return None

    cfg = _parse_env_block(content)
    key = f"KONG_BASE_URL_{env.upper()}"
    return cfg.get(key)


class KongTool(Tool):
    name = "kong"
    prompt_hint = (
        "Call application APIs that sit behind the Kong gateway; authentication is "
        "handled for you. For non-Kong third-party APIs use api_request instead."
    )
    description = (
        "Make authenticated HTTP requests through the Kong API gateway. "
        "Supports GET/POST/PUT/DELETE/PATCH with JSON body. "
        "Use for calling application APIs that are behind Kong."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["request"],
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                "default": "GET",
            },
            "path": {
                "type": "string",
                "description": "API path (e.g. /api/v1/users). Appended to base URL. Do NOT include query string here — use query_params instead.",
            },
            "query_params": {
                "type": "object",
                "description": "URL query parameters as key-value pairs (e.g. {\"companyId\": \"ACME\"}). Added to the URL by the tool — do not embed them in path.",
            },
            "base_url": {
                "type": "string",
                "description": "Override base URL (defaults to project's Kong base)",
            },
            "body": {
                "oneOf": [
                    {"type": "object"},
                    {"type": "array"},
                ],
                "description": "JSON request body (for POST/PUT/PATCH). Can be a JSON object or array.",
            },
            "env": {
                "type": "string",
                "enum": ["dev", "uat", "int"],
                "default": "dev",
                "description": "Environment determines which base URL and token to use. dev → KONG_BASE_URL_DEV / kong:dev, uat → KONG_BASE_URL_UAT / kong:uat, int → KONG_BASE_URL_INT / kong:int",
            },
            "accept": {
                "type": "string",
                "default": "application/json",
            },
            "content_type": {
                "type": "string",
                "default": "application/json",
            },
        },
        "required": ["operation", "path"],
    }

    async def _resolve_token(self, context: ToolContext, env: str) -> str | None:
        """Try project secret, then user token, then interactive prompt."""
        token_key = f"kong:{env}"

        # 1. Try project secret (project-level, then user-level fallback)
        token = await get_secret_optional(context, token_key, environment=env)
        if token:
            _log.warning(
                "kong._resolve_token: found token key=%r env=%r len=%d project_id=%r",
                token_key, env, len(token), context.project_id,
            )
            return token
        _log.warning(
            "kong._resolve_token: no token found in project_secrets or user_tokens "
            "— key=%r env=%r project_id=%r; will prompt user",
            token_key, env, context.project_id,
        )

        # 2. No token found — prompt user interactively if session is available
        session = getattr(context, "session", None)
        if not session:
            _log.warning("kong._resolve_token: no session available, cannot prompt user")
            return None

        _ENV_LABELS = {"dev": "Dev", "uat": "UAT", "int": "INT"}
        env_label = _ENV_LABELS.get(env, env.upper())
        prompt_id = str(uuid.uuid4())
        result = await session.request_user_selection(
            prompt_id=prompt_id,
            field_key=token_key,
            question=f"Kong {env_label} 环境需要 API Token，请输入：",
            kind="text",
            title=f"需要 Kong Token ({env_label})",
            message="该 token 不会发送给大模型，将直接加密保存到当前项目 Secrets，并由 Kong 工具在服务端内部使用。",
            secret=True,
            task_id=context.current_task_id,
            service_key=token_key,
            environment=env,
            save_to_project_secrets=True,
            timeout=300.0,
        )

        if result == "secret_saved":
            # The secret was saved by a separate DB session (_save_secret_from_response).
            # Our tool_db session may still hold a stale transaction snapshot (RCSI),
            # so commit (no-op) to end the implicit transaction before re-reading.
            if context.db_session:
                await context.db_session.commit()
            token = await get_secret_optional(context, token_key, environment=env)
            if not token:
                _log.warning(
                    "kong._resolve_token: secret_saved but re-read returned None "
                    "— key=%r env=%r project_id=%r",
                    token_key, env, context.project_id,
                )
            else:
                _log.warning(
                    "kong._resolve_token: secret_saved re-read OK key=%r env=%r len=%d project_id=%r",
                    token_key, env, len(token), context.project_id,
                )
            return token
        _log.warning("kong._resolve_token: secret not saved, user_selection result=%r", result)
        return None

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]
        if operation != "request":
            return {"error": f"Unknown operation: {operation}"}

        env = params.get("env", "dev")
        # Normalize legacy "dev_uat" → "dev"
        if env == "dev_uat":
            env = "dev"
        token_key = f"kong:{env}"

        token = await self._resolve_token(context, env)
        if not token:
            _log.warning("kong.execute: token resolution failed for key=%r env=%r project_id=%r", token_key, env, context.project_id)
            return {"error": f"Kong token '{token_key}' 未配置且用户未提供。请在项目密钥中配置后重试。"}

        path = params.get("path", "")
        base = (
            params.get("base_url")
            or _resolve_kong_base_from_knowledge(context, env)
        )
        if not base:
            key_name = f"KONG_BASE_URL_{env.upper()}"
            return {"error": f"No Kong base URL configured. Set {key_name} in project knowledge (environment/environment) or pass base_url parameter."}

        # The knowledge-configured base and per-call overrides are both
        # LLM-reachable (agents can write knowledge files via knowledge_rw),
        # so the full SSRF gate applies to both: always-on literal checks,
        # DNS-level check per SSRF_ENABLED (same policy as api_request —
        # validate_outbound_url would re-apply the port allowlist.
        from ._ssrf import SSRFError, validate_url
        from urllib.parse import urlparse
        try:
            validate_url(base, allow_any_port=True)
            from ._http import _is_ssrf_enabled
            if _is_ssrf_enabled():
                from ._ssrf import resolve_and_validate
                _p = urlparse(base)
                resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))
        except SSRFError as e:
            return {"error": f"URL blocked by SSRF protection: {e}"}

        # Strip any query string embedded in path and merge into query_params.
        # This guards against agents incorrectly putting ?foo=bar inside path.
        parsed_path = urlsplit(path)
        clean_path = parsed_path.path
        merged_params: dict[str, Any] = {}
        if parsed_path.query:
            for k, vs in parse_qs(parsed_path.query, keep_blank_values=False).items():
                if k:  # drop empty-key artefacts like ?=
                    merged_params[k] = vs[0] if len(vs) == 1 else vs
        if params.get("query_params"):
            merged_params.update(params["query_params"])

        url = f"{base.rstrip('/')}/{clean_path.lstrip('/')}"
        method = params.get("method", "GET").upper()

        raw_body = params.get("body")
        if isinstance(raw_body, str):
            import json as _json
            try:
                raw_body = _json.loads(raw_body)
            except (ValueError, TypeError):
                pass

        def _body_hint(b: Any) -> str:
            if b is None:
                return "none"
            if isinstance(b, dict):
                return str(list(b.keys()))
            if isinstance(b, list):
                return f"list[{len(b)}]"
            return type(b).__name__

        def _query_hint(q: Any) -> str:
            """Redact query param values — they may carry secrets."""
            if isinstance(q, dict):
                return str({k: "***" for k in q})
            if isinstance(q, list):
                return f"list[{len(q)}]"
            return type(q).__name__

        _log.warning(
            "kong.execute: %s %s | env=%r base=%r path=%r query=%s body=%s token_len=%d",
            method, url, env, base, path, _query_hint(merged_params),
            _body_hint(raw_body),
            len(token),
        )

        http = ensure_http_client(context, target_url=base)
        headers: dict[str, str] = {
            "x-api-key": token,
            "Accept": params.get("accept", "application/json"),
        }
        if method in ("POST", "PUT", "PATCH"):
            headers["Content-Type"] = params.get("content_type", "application/json")

        try:
            kwargs: dict[str, Any] = {"headers": headers}
            if merged_params:
                kwargs["params"] = merged_params
            if method in ("POST", "PUT", "PATCH") and raw_body is not None:
                kwargs["json"] = raw_body
            # Stream the body with a byte cap — a plain request() buffers the
            # whole payload before the checks below could see it, so an
            # oversized backend response would still exhaust memory.
            async with http.stream(method, url, **kwargs) as resp:
                status = resp.status_code
                response_headers = dict(resp.headers)
                content_type = response_headers.get("content-type", "")
                # Fast-fail on an explicit oversized Content-Length.
                _cl = response_headers.get("content-length", "")
                if _cl.isdigit() and int(_cl) > _MAX_RESPONSE:
                    _log.warning("kong response too large: %s %s (%s bytes)", method, url, _cl)
                    return {"error": f"Kong response too large ({_cl} bytes > {_MAX_RESPONSE})"}
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_RESPONSE:
                        _log.warning("kong response too large: %s %s (%d bytes)", method, url, total)
                        return {"error": f"Kong response too large (> {_MAX_RESPONSE} bytes)"}
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)
        except httpx.TimeoutException:
            _log.warning("kong request timeout: %s %s", method, url)
            return {"error": "Request timed out"}
        except Exception as e:
            _log.warning("kong request failed: %s %s | %s", method, url, e)
            return {"error": f"Kong request failed: {e}"}

        if "json" in content_type and total <= _MAX_RESPONSE:
            try:
                body = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except Exception:
                body = body_bytes.decode("utf-8", errors="replace")[:_MAX_RESPONSE]
        else:
            body = body_bytes.decode("utf-8", errors="replace")[:_MAX_RESPONSE]

        # gateways echo the x-api-key back in error bodies ("invalid
        # x-api-key: <token>") — scrub the resolved token before the body
        # reaches the LLM/history, mirroring api_request's redact_secrets pass.
        # JSON responses are parsed dicts/list, not str — redact_secrets
        # calls str.replace, so only apply it to text and recurse for JSON.
        if isinstance(body, str):
            body = redact_secrets(body, {token_key: token})
        elif isinstance(body, (dict, list)):
            from .api_request import _redact_secret_values
            body = _redact_secret_values(body, {token_key: token})

        result: dict[str, Any] = {
            "status": status,
            "content_type": content_type,
            "body": body,
        }
        if status >= 400:
            _log.warning("kong HTTP %d: %s %s | response: %s", status, method, url, str(body)[:500])
            result["error"] = f"HTTP {status}"
        return result
