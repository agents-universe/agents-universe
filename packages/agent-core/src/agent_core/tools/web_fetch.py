"""Web fetch tool — retrieve content from URLs with SSRF protection."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from .base import Tool, ToolContext
from ._http import ensure_http_client, validate_outbound_url
from ._ssrf import SSRFError, validate_redirect, validate_url, _MAX_REDIRECTS, _MAX_RESPONSE_BYTES

_log = logging.getLogger(__name__)


class WebFetchTool(Tool):
    name = "web_fetch"
    prompt_hint = (
        "One of the only network paths: fetch a URL's text content (HTTP GET). Shell "
        "curl/wget are blocked — never attempt them. For authenticated or non-GET API "
        "calls use api_request or kong instead."
    )
    description = "Fetch the text content of a URL via HTTP GET."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "timeout_seconds": {"type": "integer", "default": 30},
            "max_chars": {"type": "integer", "default": 50000, "description": "Truncate response at this many characters"},
        },
        "required": ["url"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = params["url"]

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return {"error": "Invalid URL: must start with http:// or https://"}

        # LLM-supplied params are strings far more often than the
        # schema implies ("30", "50000"). A string timeout hit httpx.Timeout
        # with a TypeError that escaped the RequestError handlers, and a
        # string max_chars crashed the slice. Clamp both like shell does
        # (timeout 1-120s; max_chars 1-200k).
        try:
            timeout = int(params.get("timeout_seconds", 30))
        except (TypeError, ValueError):
            timeout = 30
        timeout = max(1, min(timeout, 120))
        try:
            max_chars = int(params.get("max_chars", 50000))
        except (TypeError, ValueError):
            max_chars = 50000
        max_chars = max(1, min(max_chars, 200_000))

        # SSRF: validate initial URL. validate_url is the always-on literal
        # check (scheme/host/IP/port/metadata); validate_outbound_url adds DNS
        # resolution checks behind the SSRF_ENABLED env gate.
        try:
            validate_url(url)
            validate_outbound_url(url)
        except SSRFError as e:
            return {"error": f"Blocked by SSRF policy: {e}"}

        http = ensure_http_client(context, target_url=url)

        try:
            # Manual redirect loop — re-validate each hop
            current_url = url
            redirects = 0

            while True:
                async with http.stream(
                    "GET",
                    current_url,
                    timeout=timeout,
                    headers={"User-Agent": "AgentsUniverse/1.0"},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        redirects += 1
                        if redirects > _MAX_REDIRECTS:
                            return {"error": f"Too many redirects (max {_MAX_REDIRECTS})"}

                        location = response.headers.get("location", "")
                        if not location:
                            return {"error": "Redirect with no Location header"}

                        # Resolve relative redirects (path-relative, query-only, protocol-relative)
                        from urllib.parse import urljoin, urlparse
                        resolved_location = urljoin(current_url, location)
                        parsed = urlparse(resolved_location)
                        if parsed.scheme not in ("http", "https"):
                            return {"error": f"Blocked redirect to non-http URL: {location}"}
                        location = resolved_location

                        # SSRF: validate redirect target
                        try:
                            validate_redirect(location)
                            validate_outbound_url(location)
                        except SSRFError as e:
                            return {"error": f"Redirect blocked by SSRF policy: {e}"}

                        current_url = location
                        continue

                    # Non-redirect response — read body with a hard size limit.
                    # content-length is only a fast-path pre-check; chunked or
                    # absent-length bodies are enforced by streaming and aborting
                    # past _MAX_RESPONSE_BYTES instead of buffering them whole.
                    content_length = response.headers.get("content-length")
                    try:
                        clen = int(content_length) if content_length else 0
                    except (TypeError, ValueError):
                        clen = 0
                    if clen > _MAX_RESPONSE_BYTES:
                        return {"error": f"Response too large: {content_length} bytes (max {_MAX_RESPONSE_BYTES})"}

                    total = 0
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            return {"error": f"Response too large (exceeded {_MAX_RESPONSE_BYTES} bytes); download aborted"}
                        chunks.append(chunk)
                    text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")

                    content = text[:max_chars]
                    result = {
                        "content": content,
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type", ""),
                        "url": current_url,
                        "truncated": len(text) > max_chars,
                    }
                    if redirects > 0:
                        result["redirects"] = redirects
                    if response.status_code >= 400:
                        _log.warning("web_fetch HTTP %d: %s", response.status_code, current_url)
                        return {"error": f"HTTP {response.status_code}", "url": current_url, "status_code": response.status_code}
                    return result

        except httpx.TimeoutException:
            _log.warning("web_fetch timeout after %ds: %s", timeout, url)
            return {"error": f"Request timed out after {timeout}s: {url}"}
        except httpx.RequestError as e:
            _log.warning("web_fetch request error for %s: %s", url, e)
            return {"error": f"Request failed: {e}"}
