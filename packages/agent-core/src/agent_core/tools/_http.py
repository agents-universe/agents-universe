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


class _SSRFPinningTransport(httpx.AsyncBaseTransport):
    """Close the DNS-rebinding TOCTOU window on agent-facing requests.

    The tools validate a URL (DNS included) BEFORE the request, but httpx
    re-resolves the hostname at connect time - a rebinding DNS server can
    answer the validation lookup with a public IP and the connect lookup
    with 127.0.0.1. This transport re-resolves at request time and pins the
    connection to a validated IP: the URL host is rewritten to the IP, while
    the Host header and TLS SNI (httpcore's sni_hostname extension) keep the
    original name so vhosts and certificate validation behave normally.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _is_ssrf_enabled():
            await self._pin(request)
        return await self._inner.handle_async_request(request)

    async def _pin(self, request: httpx.Request) -> None:
        import asyncio
        import ipaddress

        url = request.url
        host = url.host
        if not host:
            return
        # Literal IPs were already validated by the call site's validate_url;
        # port/scheme checks likewise happened pre-request (several call
        # sites legitimately use non-allowlisted ports, so no re-check here).
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        port = url.port or (443 if url.scheme == "https" else 80)
        # getaddrinfo is blocking - keep it off the event loop.
        safe_ips = await asyncio.to_thread(resolve_and_validate, host, port)
        ip = safe_ips[0]
        # Request.url is a plain attribute - reassigning it before the inner
        # transport sees the request redirects the connection; the proxy
        # CONNECT target and Host header derive from what we set here.
        # copy_with(host=...) brackets IPv6 and preserves the URL's own port;
        # copy_with(netloc=...) cannot parse bracketed IPv6.
        request.url = url.copy_with(host=ip)
        request.headers["Host"] = host if (port == 80 or port == 443) else f"{host}:{port}"
        request.extensions["sni_hostname"] = host


def _ssrf_transport_kwargs(ssl_verify: bool, proxy: str | None) -> dict:
    """Transport kwargs for ensure_http_client's clients.

    With SSRF enabled, wrap the real transport in the pinning layer and move
    the proxy onto the inner transport (httpx ignores proxy= when a custom
    transport is passed). Disabled -> proxy as a plain client kwarg.
    """
    if not _is_ssrf_enabled():
        return {"proxy": proxy} if proxy else {}
    return {
        "transport": _SSRFPinningTransport(
            httpx.AsyncHTTPTransport(verify=ssl_verify, proxy=proxy)
        )
    }


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
        # When SSRF is enabled the pinning transport wraps the real one -
        # httpx ignores proxy= alongside a custom transport, so the proxy
        # must live on the INNER transport (none here - this client is
        # explicitly non-proxied).
        owner.http_client_no_proxy = httpx.AsyncClient(
            verify=ssl_verify,
            timeout=_DEFAULT_TIMEOUT,
            trust_env=False,
            max_redirects=0,
            **_ssrf_transport_kwargs(ssl_verify, None),
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
        max_redirects=0,
        **_ssrf_transport_kwargs(ssl_verify, proxy),
    )
    context.http_client = owner.http_client
    return owner.http_client
