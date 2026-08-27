"""Tests for POST /api/tokens/{service_key}/test — unknown providers
(kong:*, jira:email, custom keys) must return the "saved" fallback instead
of a null response body."""
from __future__ import annotations

import httpx


async def test_token_test_plain_keys_return_saved(client):
    resp = await client.put("/api/tokens/kong:dev", json={"value": "s3cret"})
    assert resp.status_code == 200

    resp = await client.post("/api/tokens/kong:dev/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "provider": "kong", "note": "saved"}


async def test_token_test_subkey_returns_saved(client):
    resp = await client.put("/api/tokens/jira:email", json={"value": "me@example.com"})
    assert resp.status_code == 200

    resp = await client.post("/api/tokens/jira:email/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["note"] == "saved"


async def test_token_test_missing_token_404(client):
    resp = await client.post("/api/tokens/never-configured/test")
    assert resp.status_code == 404


def _echoing_error(key: str, echo: str) -> httpx.Response:
    """A 401 response whose body echoes the submitted credential back —
    exactly what real providers/gateways do."""
    return httpx.Response(
        401,
        json={"error": {"message": echo}},
        request=httpx.Request("POST", "http://test/invoke"),
    )


class _EchoingClient:
    """AsyncClient stand-in: post()/get() return the pre-built response."""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self.response

    async def get(self, *a, **k):
        return self.response


class _RaisingClient:
    """AsyncClient stand-in: its post() raises an exception whose str() embeds
    the key (httpx/SDK behavior on transport errors)."""

    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise self.exc


async def test_openai_test_error_does_not_leak_key(client, monkeypatch):
    """OpenAI's 401 body echoes the key ('Incorrect API key provided: sk-...')
    — the tokens test endpoint must scrub it via _redact_key before returning."""
    key = "sk-leakme-1234567890"
    resp = await client.put("/api/tokens/openai", json={"value": key})
    assert resp.status_code == 200

    resp401 = _echoing_error(
        key, f"Incorrect API key provided: {key}. Find it at https://platform.openai.com/api-keys"
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _EchoingClient(resp401))

    resp = await client.post("/api/tokens/openai/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    # The key value must not appear anywhere in the response.
    assert key not in str(body)
    assert "leakme" not in str(body)
    assert "[REDACTED]" in str(body)


async def test_openai_test_exception_does_not_leak_key(client, monkeypatch):
    """A raised exception whose str() embeds the key (httpx/SDK behavior)
    must be scrubbed by the except fallback before returning."""
    key = "sk-leakme-9876543210"
    resp = await client.put("/api/tokens/openai", json={"value": key})
    assert resp.status_code == 200

    exc = RuntimeError(f"Incorrect API key provided: {key}")
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _RaisingClient(exc))

    resp = await client.post("/api/tokens/openai/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert key not in str(body)
    assert "leakme" not in str(body)


async def test_anthropic_test_error_does_not_leak_key(client, monkeypatch):
    """Anthropic's 401 body can echo the key as x-api-key — scrub it too."""
    key = "sk-ant-leakme-12345"
    resp = await client.put("/api/tokens/anthropic", json={"value": key})
    assert resp.status_code == 200

    resp401 = _echoing_error(key, f"authentication_error: invalid x-api-key {key}")
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _EchoingClient(resp401))

    resp = await client.post("/api/tokens/anthropic/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert key not in str(body)
    assert "[REDACTED]" in str(body)


async def test_jira_test_error_does_not_leak_credential(client, monkeypatch):
    """Jira's 401 body echoes the credential ('Bad credentials: <token>') —
    the shared test_service_token path must scrub both the token and email."""
    key = "atlassian-token-leakme-12345"
    email = "leakme@example.com"
    resp = await client.put(
        "/api/tokens/jira",
        json={"value": key, "base_url": "https://example.atlassian.net"},
    )
    assert resp.status_code == 200
    resp = await client.put("/api/tokens/jira:email", json={"value": email})
    assert resp.status_code == 200

    resp401 = _echoing_error(
        key,
        f"Bad credentials: user {email} token {key} is invalid",
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _EchoingClient(resp401))

    resp = await client.post("/api/tokens/jira/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert key not in str(body)
    assert "leakme" not in str(body)
    assert "[REDACTED]" in str(body)


async def test_jira_test_exception_does_not_leak_credential(client, monkeypatch):
    """The except fallback in test_service_token must scrub str(exc) too."""
    key = "atlassian-token-leakme-67890"
    email = "leakme2@example.com"
    resp = await client.put(
        "/api/tokens/jira",
        json={"value": key, "base_url": "https://example.atlassian.net"},
    )
    assert resp.status_code == 200
    resp = await client.put("/api/tokens/jira:email", json={"value": email})
    assert resp.status_code == 200

    exc = RuntimeError(f"Bad credentials: {email} token {key} is invalid")
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _RaisingClient(exc))

    resp = await client.post("/api/tokens/jira/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert key not in str(body)
    assert "leakme2" not in str(body)
