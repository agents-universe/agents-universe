"""Tests for POST /api/tokens... (provider API key) test endpoint — the
raw SDK exception must never leak the key value back to the client."""

from __future__ import annotations


async def test_api_key_test_does_not_leak_key_in_error(client):
    """OpenAI's 401 body echoes the key ('Incorrect API key provided: sk-...')
    — the endpoint must return a sanitized error, not the SDK exception text."""
    # Configure a key.
    resp = await client.put("/api/api-keys/openai", json={"value": "sk-leakme-1234567890"})
    assert resp.status_code == 200

    class _FakeOpenAIError(Exception):
        def __str__(self):
            return "Incorrect API key provided: sk-leakme-1234567890. You can find your API key at https://platform.openai.com/api-keys"

    import api.routers.api_keys as mod

    class _FakeAsyncOpenAI:
        def __init__(self, *a, **k):
            pass

        async def models(self):
            raise _FakeOpenAIError("bad key")

    original = mod.AsyncOpenAI if hasattr(mod, "AsyncOpenAI") else None
    mod.AsyncOpenAI = _FakeAsyncOpenAI
    try:
        resp = await client.post("/api/api-keys/openai/test")
    finally:
        if original:
            mod.AsyncOpenAI = original
        else:
            del mod.AsyncOpenAI

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    # The key value must not appear anywhere in the response.
    assert "sk-leakme-1234567890" not in str(body)
    assert "leakme" not in str(body)
    # A useful, generic error is returned.
    assert body["error"]
