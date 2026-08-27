"""Tests for the /api/integrations proxy endpoints.

The Kong proxy forwards the user's x-api-key to the gateway and returns the
gateway's response body verbatim — a gateway error body that echoes the
submitted key back ("invalid x-api-key: <token>") must be scrubbed before
the body reaches the frontend (same rule as kong.py / api_keys.py / tokens.py).
"""
from __future__ import annotations

import httpx


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in returning a fixed response."""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return self.response

    async def post(self, *a, **k):
        return self.response

    async def put(self, *a, **k):
        return self.response

    async def delete(self, *a, **k):
        return self.response


def _json_response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://test/x"))


async def test_kong_proxy_scrubs_echoed_key_from_json_error(client, monkeypatch):
    """A 401 gateway body echoing the x-api-key must not leak the key to the
    frontend through the JSON body path."""
    key = "kong-key-leakme-12345"
    resp = await client.put("/api/tokens/kong:dev", json={"value": key})
    assert resp.status_code == 200

    gateway_body = {"message": f"invalid x-api-key: {key}", "status": 401}
    fake = _FakeAsyncClient(_json_response(401, gateway_body))
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: fake)

    resp = await client.post(
        "/api/integrations/kong/request",
        json={"method": "GET", "path": "/some/api", "env": "dev"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 401
    assert key not in str(body)
    assert "leakme" not in str(body)
    assert "invalid x-api-key: [REDACTED]" in str(body)


async def test_kong_proxy_scrubs_echoed_key_from_plaintext_error(client, monkeypatch):
    """Non-JSON gateway error bodies (raise_for_status path) also echo the key
    — scrub before returning."""
    key = "kong-key-leakme-67890"
    resp = await client.put("/api/tokens/kong:dev", json={"value": key})
    assert resp.status_code == 200

    # A non-JSON 401 body that echoes the key, returned via HTTPStatusError.
    http_resp = httpx.Response(
        401,
        text=f"invalid x-api-key: {key}\n",
        request=httpx.Request("GET", "http://test/x"),
    )

    class _RaisingClient(_FakeAsyncClient):
        async def get(self, *a, **k):
            raise httpx.HTTPStatusError("401 Unauthorized", request=http_resp.request, response=http_resp)

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _RaisingClient(None))

    resp = await client.post(
        "/api/integrations/kong/request",
        json={"method": "GET", "path": "/some/api", "env": "dev"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 401
    assert key not in str(body)
    assert "leakme" not in str(body)


async def test_kong_proxy_success_body_keeps_content(client, monkeypatch):
    """Successful responses pass through unchanged (no false redaction)."""
    key = "kong-key-keepme"
    resp = await client.put("/api/tokens/kong:dev", json={"value": key})
    assert resp.status_code == 200

    gateway_body = {"data": "hello world"}
    fake = _FakeAsyncClient(_json_response(200, gateway_body))
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: fake)

    resp = await client.post(
        "/api/integrations/kong/request",
        json={"method": "GET", "path": "/some/api", "env": "dev"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": 200, "body": {"data": "hello world"}}
