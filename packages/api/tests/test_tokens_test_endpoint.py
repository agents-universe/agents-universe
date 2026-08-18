"""Tests for POST /api/tokens/{service_key}/test — unknown providers
(kong:*, jira:email, custom keys) must return the "saved" fallback instead
of a null response body."""
from __future__ import annotations


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
