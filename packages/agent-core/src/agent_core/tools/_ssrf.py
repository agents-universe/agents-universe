"""SSRF protection — validate URLs and resolved IPs before outbound requests.

Blocks:
- Non-http(s) schemes
- Private/loopback/link-local/multicast/metadata IP ranges (after DNS resolution)
- Restricted ports
- Excessive redirects (each hop re-validated)
- Oversized responses
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# AWS/GCP/Azure metadata endpoints
_METADATA_IPS = frozenset({
    "169.254.169.254",
    "fd00:ec2::254",
    "metadata.google.internal",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Frontend dev servers (Vite 5173/4173/5174, others) are legitimate targets
# for browser/web_fetch tools even with SSRF_ENABLED=false — without them in
# the allowlist an agent can never open its own dev frontend.
_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443, 8000, 3000, 5000, 9090, 5173, 4173, 5174})

_MAX_REDIRECTS = 5

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


class SSRFError(Exception):
    """Raised when a URL or resolved IP fails SSRF validation."""
    pass


def _is_ip_blocked(ip_str: str) -> bool:
    """Check if an IP address belongs to a blocked range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Unparseable = blocked

    if addr.is_loopback:
        return True
    if addr.is_private:
        return True
    if addr.is_link_local:
        return True
    if addr.is_multicast:
        return True
    if addr.is_reserved:
        return True
    if addr.is_unspecified:
        return True

    # IPv4-mapped IPv6 — check the embedded v4
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return _is_ip_blocked(str(addr.ipv4_mapped))

    # Cloud metadata (169.254.169.254)
    if ip_str in _METADATA_IPS:
        return True

    return False


def validate_url(url: str, *, allow_any_port: bool = False) -> None:
    """Validate URL scheme, host, and port BEFORE making a request.

    Raises SSRFError if the URL is not safe to fetch.
    This does NOT resolve DNS — call validate_resolved_ip() after resolution.
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"Blocked scheme: {parsed.scheme!r} (allowed: http, https)")

    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no hostname")

    # Block metadata hostnames
    if host.lower() in _METADATA_IPS:
        raise SSRFError(f"Blocked metadata host: {host}")

    # Check if host is a literal IP address
    try:
        ipaddress.ip_address(host)
        # It IS an IP literal — validate it
        if _is_ip_blocked(host):
            raise SSRFError(f"Blocked IP in URL: {host}")
    except ValueError:
        pass  # Not an IP literal (it's a hostname) — will be resolved later

    # Port check — `parsed.port` raises ValueError on non-numeric
    # or out-of-range ports ("http://h:abc/", "http://h:99999/"); callers
    # catch SSRFError only, so the error escaped the tool and surfaced as a
    # cryptic exception string. Normalize to the structured error.
    try:
        port = parsed.port
    except ValueError as exc:
        raise SSRFError(f"Invalid port in URL: {exc}") from exc
    if port is not None and not allow_any_port and port not in _ALLOWED_PORTS:
        raise SSRFError(f"Blocked port: {port} (allowed: {sorted(_ALLOWED_PORTS)})")


def validate_resolved_ip(ip_str: str, original_url: str = "") -> None:
    """Validate a resolved IP address. Call after DNS resolution.

    Raises SSRFError if the IP is in a blocked range.
    """
    if _is_ip_blocked(ip_str):
        raise SSRFError(
            f"DNS resolved to blocked IP: {ip_str}"
            + (f" (from {original_url})" if original_url else "")
        )


def resolve_and_validate(hostname: str, port: int = 443) -> list[str]:
    """Resolve hostname via DNS and validate all returned IPs.

    Returns list of safe IP addresses.
    Raises SSRFError if ALL resolved IPs are blocked, or resolution fails.
    """
    try:
        results = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"DNS resolution failed for {hostname}: {e}")

    if not results:
        raise SSRFError(f"DNS returned no results for {hostname}")

    safe_ips = []
    for family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if not _is_ip_blocked(ip):
            safe_ips.append(ip)

    if not safe_ips:
        blocked = [sockaddr[0] for _, _, _, _, sockaddr in results]
        raise SSRFError(
            f"All resolved IPs for {hostname} are blocked: {blocked}"
        )

    return safe_ips


def validate_redirect(new_url: str, *, allow_any_port: bool = False) -> None:
    """Validate a redirect target URL. Same rules as validate_url."""
    validate_url(new_url, allow_any_port=allow_any_port)
