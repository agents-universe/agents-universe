"""SSRF IP-pinning transport: closes the DNS-rebinding TOCTOU window on the
shared agent HTTP client (see agent_core.tools._http._SSRFPinningTransport)."""

import httpx
import pytest

from agent_core.tools import _http


def _make_request(url: str) -> httpx.Request:
    return httpx.Request("GET", url)


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Records the request it received and returns a stub response."""

    def __init__(self) -> None:
        self.seen: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen = request
        return httpx.Response(200, request=request, text="ok")


@pytest.mark.asyncio
async def test_pinning_rewrites_host_keeps_sni_and_host_header(monkeypatch):
    def fake_resolve(hostname, port):
        assert hostname == "api.example.com"
        assert port == 443
        return ["203.0.113.10"]

    monkeypatch.setattr(_http, "resolve_and_validate", fake_resolve)
    monkeypatch.setattr(_http, "_is_ssrf_enabled", lambda: True)

    inner = _RecordingTransport()
    transport = _http._SSRFPinningTransport(inner)
    request = _make_request("https://api.example.com/v1/x?k=1")
    await transport.handle_async_request(request)

    assert inner.seen is not None
    # Connection target: the validated IP
    assert inner.seen.url.host == "203.0.113.10"
    # Everything else preserved: scheme, port (implicit), path, query
    assert inner.seen.url.path == "/v1/x"
    assert inner.seen.url.query == b"k=1"
    # Host header and TLS SNI keep the original name (vhosts + cert checks)
    assert inner.seen.headers["Host"] == "api.example.com"
    assert inner.seen.extensions["sni_hostname"] == "api.example.com"


@pytest.mark.asyncio
async def test_pinning_skips_ip_literals_and_non_standard_ports(monkeypatch):
    def fake_resolve(hostname, port):
        return ["198.51.100.7"]

    monkeypatch.setattr(_http, "resolve_and_validate", fake_resolve)
    monkeypatch.setattr(_http, "_is_ssrf_enabled", lambda: True)

    inner = _RecordingTransport()
    transport = _http._SSRFPinningTransport(inner)

    # Literal IP: validated pre-request, transport must not re-resolve it.
    literal = _make_request("http://198.51.100.9:8080/x")
    await transport.handle_async_request(literal)
    assert inner.seen.url.host == "198.51.100.9"

    # Non-standard port rides along on the rewritten netloc and the Host
    # header keeps the original name:port.
    request = _make_request("https://api.example.com:8443/x")
    await transport.handle_async_request(request)
    assert inner.seen.url.host == "198.51.100.7"
    assert inner.seen.url.port == 8443
    assert inner.seen.headers["Host"] == "api.example.com:8443"


@pytest.mark.asyncio
async def test_pinning_disabled_passthrough(monkeypatch):
    called = False

    def fail_resolve(hostname, port):  # pragma: no cover - must not run
        nonlocal called
        called = True
        return ["203.0.113.10"]

    monkeypatch.setattr(_http, "resolve_and_validate", fail_resolve)
    monkeypatch.setattr(_http, "_is_ssrf_enabled", lambda: False)

    inner = _RecordingTransport()
    transport = _http._SSRFPinningTransport(inner)
    request = _make_request("https://api.example.com/x")
    await transport.handle_async_request(request)

    assert not called
    assert inner.seen.url.host == "api.example.com"
