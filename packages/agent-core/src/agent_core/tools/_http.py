"""Shared HTTP client factory for agent tools.

Configures proxy, SSL, timeout, and SSRF protection from environment.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from .base import ToolContext
from ._ssrf import SSRFError, resolve_and_validate, validate_url, validate_redirect

_DEFAULT_TIMEOUT = 60.0


def _should_bypass_proxy(target_url: str, no_proxy: str) -> bool:
    """Check if target_url matches any entry in the NO_PROXY list."""
    if not no_proxy or not target_url:
        return False
    host = urlparse(target_url).hostname or ""
    if not host:
        return False
    for pattern in no_proxy.split(","):
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if host == pattern or host.endswith("." + pattern.lstrip(".")):
            return True
    return False


def _is_ssrf_enabled() -> bool:
    """SSRF protection is OFF by default. Set SSRF_ENABLED=true to enable."""
    return os.environ.get("SSRF_ENABLED", "").lower() in ("1", "true", "yes")


def validate_outbound_url(url: str) -> None:
    """Validate a URL for SSRF safety before making an outbound request.

    SSRF protection is disabled by default. Enable via SSRF_ENABLED=true env var.
    When enabled, checks scheme, port, and resolves DNS to verify the target IP
    is not in a blocked range (private, loopback, link-local, metadata, etc.).

    Raises SSRFError if the URL is unsafe.
    """
    if not _is_ssrf_enabled():
        return
    validate_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname
    if hostname:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolve_and_validate(hostname, port)


def ensure_http_client(context: ToolContext, target_url: str = "") -> httpx.AsyncClient:
    """Return the shared httpx client from context, creating one if needed.

    Uses a generous default timeout since various tools (Kong, Confluence)
    may need longer than 30s. Individual requests can override with
    httpx per-request timeout if needed.

    If target_url matches NO_PROXY, a separate non-proxied client is used.

    NOTE: This returns the shared client. Callers should use
    validate_outbound_url() BEFORE making requests to untrusted URLs.
    Internal/trusted URLs (LLM providers, configured integrations) may
    skip SSRF validation.
    """
    no_proxy = context.cfg("NO_PROXY") or os.environ.get("no_proxy", "")
    bypass = _should_bypass_proxy(target_url, no_proxy)

    ssl_verify = context.ssl_verify

    # Lazy resources live on the SHARED context (copy_for_task's owner
    # reference): a task clone must reuse the session's client, or every
    # parallel task opens its own connection pool that cleanup() never sees.
    owner = getattr(context, "_shared", None) or context

    if bypass:
        if owner.http_client_no_proxy is not None:
            context.http_client_no_proxy = owner.http_client_no_proxy
            return owner.http_client_no_proxy
        owner.http_client_no_proxy = httpx.AsyncClient(
            verify=ssl_verify,
            timeout=_DEFAULT_TIMEOUT,
            trust_env=False,
            max_redirects=0,
        )
        context.http_client_no_proxy = owner.http_client_no_proxy
        return owner.http_client_no_proxy

    if owner.http_client is not None:
        context.http_client = owner.http_client
        return owner.http_client

    proxy = context.cfg("HTTPS_PROXY") or os.environ.get("https_proxy")
    owner.http_client = httpx.AsyncClient(
        verify=ssl_verify,
        timeout=_DEFAULT_TIMEOUT,
        proxy=proxy,
        max_redirects=0,
    )
    context.http_client = owner.http_client
    return owner.http_client
