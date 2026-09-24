"""Tests for /api/model-configs CRUD + complexity_tier inference."""
from __future__ import annotations

import httpx
import pytest


async def _create(client, **overrides):
    body = {
        "provider": "anthropic",
        "model_id": "claude-sonnet-5",
        **overrides,
    }
    resp = await client.post("/api/model-configs", json=body)
    return resp


async def test_create_infers_tier_from_model_id(client):
    resp = await _create(client, model_id="claude-haiku-4-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["complexity_tier"] == "low"
    assert data["is_system"] is False


async def test_create_mid_and_high_inference(client):
    low = await _create(client, model_id="claude-sonnet-5")
    assert low.json()["complexity_tier"] == "mid"
    high = await _create(client, model_id="claude-opus-5")
    assert high.json()["complexity_tier"] == "high"


async def test_create_explicit_tier_overrides_inference(client):
    resp = await _create(client, model_id="claude-sonnet-5", complexity_tier="high")
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] == "high"


async def test_create_azure_never_infers(client):
    resp = await _create(
        client,
        provider="azure_openai",
        model_id="my-deployment",
        base_url="https://example.openai.azure.com",
    )
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] is None


async def test_create_unknown_model_gets_null_tier(client):
    resp = await _create(client, model_id="my-custom-model")
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] is None


async def test_create_invalid_tier_rejected(client):
    resp = await _create(client, model_id="claude-sonnet-5", complexity_tier="ultra")
    assert resp.status_code == 422


async def test_create_context_window_override_and_default(client):
    resp = await _create(client, model_id="claude-sonnet-5", context_window=500_000)
    assert resp.status_code == 200
    data = resp.json()
    assert data["context_window"] == 500_000
    # Name-matched default accompanies every config for the Settings prefill.
    assert data["default_context_window"] == 1_000_000


async def test_create_context_window_null_gets_name_matched_default(client):
    resp = await _create(client, model_id="claude-haiku-4-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["context_window"] is None
    assert data["default_context_window"] == 200_000


async def test_create_invalid_context_window_rejected(client):
    resp = await _create(client, model_id="claude-sonnet-5", context_window=0)
    assert resp.status_code == 422
    resp = await _create(client, model_id="claude-sonnet-5", context_window=3_000_000)
    assert resp.status_code == 422


async def test_update_context_window_and_clear_it(client):
    created = (await _create(client, model_id="claude-sonnet-5", context_window=500_000)).json()
    cid = created["config_id"]

    resp = await client.put(f"/api/model-configs/{cid}", json={"context_window": 700_000})
    assert resp.status_code == 200
    assert resp.json()["context_window"] == 700_000

    # Explicit null clears the override → back to the name-matched default.
    resp = await client.put(f"/api/model-configs/{cid}", json={"context_window": None})
    assert resp.status_code == 200
    assert resp.json()["context_window"] is None


async def test_update_without_context_window_field_keeps_existing(client):
    created = (await _create(client, model_id="claude-sonnet-5", context_window=900_000)).json()
    resp = await client.put(
        f"/api/model-configs/{created['config_id']}", json={"model_id": "claude-sonnet-5"}
    )
    assert resp.status_code == 200
    assert resp.json()["context_window"] == 900_000


async def test_list_includes_context_window_and_default(client):
    # Session-scoped DB: earlier tests created other claude-sonnet-5 rows, so
    # match on the context_window we just wrote, not on the model id.
    await _create(client, model_id="claude-sonnet-5", context_window=500_000)
    resp = await client.get("/api/model-configs")
    assert resp.status_code == 200
    cfg = next(c for c in resp.json() if c["context_window"] == 500_000)
    assert cfg["model_id"] == "claude-sonnet-5"
    assert cfg["default_context_window"] == 1_000_000


async def test_list_returns_tier(client):
    await _create(client, model_id="claude-sonnet-5", complexity_tier="low")
    resp = await client.get("/api/model-configs")
    assert resp.status_code == 200
    configs = resp.json()
    assert any(c["model_id"] == "claude-sonnet-5" and c["complexity_tier"] == "low" for c in configs)


async def test_update_changes_tier_and_can_clear_it(client):
    created = (await _create(client, model_id="claude-sonnet-5")).json()
    cid = created["config_id"]

    resp = await client.put(f"/api/model-configs/{cid}", json={"complexity_tier": "low"})
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] == "low"

    # Explicit null clears the tier.
    resp = await client.put(f"/api/model-configs/{cid}", json={"complexity_tier": None})
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] is None


async def test_update_without_tier_field_keeps_existing(client):
    created = (await _create(client, model_id="claude-sonnet-5", complexity_tier="high")).json()
    cid = created["config_id"]

    resp = await client.put(f"/api/model-configs/{cid}", json={"model_id": "claude-sonnet-5"})
    assert resp.status_code == 200
    assert resp.json()["complexity_tier"] == "high"


async def test_update_invalid_tier_rejected(client):
    created = (await _create(client, model_id="claude-sonnet-5")).json()
    resp = await client.put(
        f"/api/model-configs/{created['config_id']}", json={"complexity_tier": "bogus"}
    )
    assert resp.status_code == 422


# ── thinking_enabled / reasoning_effort ─────────────────────────────────


async def test_create_echoes_thinking_and_effort_defaults(client):
    resp = await _create(client, model_id="claude-sonnet-5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["thinking_enabled"] is None
    assert data["reasoning_effort"] is None


async def test_create_with_thinking_override(client):
    resp = await _create(client, model_id="claude-sonnet-5", thinking_enabled=False)
    assert resp.status_code == 200
    assert resp.json()["thinking_enabled"] is False

    resp = await _create(client, model_id="claude-opus-5", thinking_enabled=True)
    assert resp.json()["thinking_enabled"] is True


async def test_create_effort_normalized(client):
    # Lowercase/strip normalization: "HIGH" stores as "high".
    resp = await _create(client, provider="openai", model_id="gpt-5", reasoning_effort="HIGH")
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] == "high"


async def test_create_invalid_effort_rejected(client):
    resp = await _create(client, provider="openai", model_id="gpt-5", reasoning_effort="ultra")
    assert resp.status_code == 422
    resp = await _create(client, provider="openai", model_id="gpt-5", reasoning_effort="")
    assert resp.status_code == 422


async def test_update_thinking_tri_state(client):
    """Omit → unchanged; explicit false → set; explicit null → cleared.
    The null-clear must go through model_fields_set, not `is not None`."""
    created = (await _create(client, model_id="claude-sonnet-5")).json()
    cid = created["config_id"]

    # Set to false.
    resp = await client.put(f"/api/model-configs/{cid}", json={"thinking_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["thinking_enabled"] is False

    # Field omitted → keeps false.
    resp = await client.put(f"/api/model-configs/{cid}", json={"model_id": "claude-sonnet-5"})
    assert resp.status_code == 200
    assert resp.json()["thinking_enabled"] is False

    # Explicit null → cleared back to env default.
    resp = await client.put(f"/api/model-configs/{cid}", json={"thinking_enabled": None})
    assert resp.status_code == 200
    assert resp.json()["thinking_enabled"] is None


async def test_update_effort_tri_state(client):
    created = (await _create(client, provider="openai", model_id="gpt-5")).json()
    cid = created["config_id"]

    resp = await client.put(f"/api/model-configs/{cid}", json={"reasoning_effort": "high"})
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] == "high"

    # Omitted → unchanged.
    resp = await client.put(f"/api/model-configs/{cid}", json={"model_id": "gpt-5"})
    assert resp.json()["reasoning_effort"] == "high"

    # Explicit null → cleared.
    resp = await client.put(f"/api/model-configs/{cid}", json={"reasoning_effort": None})
    assert resp.json()["reasoning_effort"] is None

    # Invalid value on update → 422, existing value untouched.
    resp = await client.put(f"/api/model-configs/{cid}", json={"reasoning_effort": "bogus"})
    assert resp.status_code == 422


async def test_system_default_entry_carries_thinking_fields(client, monkeypatch):
    """_system_default_entry must expose the two fields (always null) or the
    TS ModelConfig type lies for the synthetic system row."""
    from types import SimpleNamespace

    from api.routers import model_configs as mod

    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(
        system_default_model_id="gpt-4o",
        system_default_api_key="sk-system-default-key",
        system_default_base_url=None,
    ))
    entry = mod._system_default_entry()
    assert entry is not None
    assert entry["thinking_enabled"] is None
    assert entry["reasoning_effort"] is None

    resp = await client.get("/api/model-configs")
    assert resp.status_code == 200
    for cfg in resp.json():
        assert "thinking_enabled" in cfg
        assert "reasoning_effort" in cfg


async def test_created_config_threads_thinking_into_cred(client, db):
    """load_model_credentials must surface the per-config overrides into the
    provider-agnostic cred dict (the sole channel to agent-core)."""
    resp = await _create(
        client,
        provider="openai",
        model_id="gpt-5",
        api_key="sk-thread-test-000111222",
        thinking_enabled=False,
        reasoning_effort="high",
    )
    assert resp.status_code == 200
    cid = resp.json()["config_id"]

    from api.services.model_credentials import load_model_credentials

    creds, _tier_models, _tier_map, _fixed = await load_model_credentials(db, "test-user")
    cred = creds[cid]
    assert cred["thinking_enabled"] is False
    assert cred["reasoning_effort"] == "high"

    # A config without overrides must omit both keys entirely → constructors
    # fall back to env defaults (byte-identical requests to pre-feature runs).
    resp2 = await _create(client, model_id="claude-sonnet-5", api_key="sk-thread-test-333444555")
    cid2 = resp2.json()["config_id"]
    creds2, *_ = await load_model_credentials(db, "test-user")
    assert "thinking_enabled" not in creds2[cid2]
    assert "reasoning_effort" not in creds2[cid2]


async def test_delete(client):
    created = (await _create(client, model_id="claude-sonnet-5")).json()
    resp = await client.delete(f"/api/model-configs/{created['config_id']}")
    assert resp.status_code == 200
    resp = await client.get("/api/model-configs")
    assert all(c["config_id"] != created["config_id"] for c in resp.json())


async def test_user_isolation(client, as_user):
    """One user's configs must never leak into another user's list."""
    await _create(client, model_id="claude-sonnet-5")
    async with as_user("other-user"):
        resp = await client.get("/api/model-configs")
        assert resp.status_code == 200
        # Only the virtual system-default entry (is_system) may appear — never
        # another user's saved configs.
        assert [c for c in resp.json() if not c["is_system"]] == []


class _EchoingAsyncClient:
    """httpx.AsyncClient stand-in returning a fixed 401 response."""

    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self.response


async def test_test_endpoint_does_not_leak_api_key(client, monkeypatch):
    """A provider 401 body echoing the api_key must be scrubbed before the
    error reaches the client (same rule as api_keys.py / tokens.py)."""
    key = "sk-leakme-000011112222"
    created = (
        await _create(client, model_id="claude-haiku-4-5", api_key=key)
    ).json()
    cid = created["config_id"]

    resp401 = httpx.Response(
        401,
        json={"error": {"message": f"Incorrect API key provided: {key}"}},
        request=httpx.Request("POST", "http://test/invoke"),
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _EchoingAsyncClient(resp401))

    resp = await client.post(f"/api/model-configs/{cid}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert key not in str(body)
    assert "leakme" not in str(body)
    assert "[REDACTED]" in str(body)


class _ProtocolErrorAsyncClient:
    """httpx.AsyncClient stand-in whose request raises the h11 echo of a key.

    h11 rejects an illegal header value by echoing it back verbatim —
    "Illegal header value b'...'" — and for a header-held api_key that echo
    IS the credential.
    """

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        raise httpx.LocalProtocolError("Illegal header value b'Bearer sk-h11-echo\\n'")

    async def __aexit__(self, *a):
        return False


class _LeakyAsyncClient:
    """httpx.AsyncClient stand-in whose request raises an error quoting the key."""

    def __init__(self, key, *a, **k):
        self.key = key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError(f"upstream rejected Authorization: Bearer {self.key}")


async def test_test_endpoint_refuses_header_unsafe_key(monkeypatch, caplog):
    """A pasted trailing newline makes the key illegal as a header value.
    _do_test must refuse before the request, with a message that names the
    problem but never the key itself — pre-fix it reached h11, which echoed
    the whole key into the logged traceback."""
    import logging

    from api.routers.model_configs import _do_test

    key = "sk-h11-echo\n"
    escaped = repr(key.encode("utf-8"))[2:-1]
    monkeypatch.setattr("httpx.AsyncClient", _ProtocolErrorAsyncClient)

    with caplog.at_level(logging.DEBUG):
        result = await _do_test("openai", "gpt-4o", key, None)

    assert result["ok"] is False
    assert "control character" in result["error"]
    assert key not in result["error"]
    assert escaped not in result["error"]
    # Nothing was logged — and if something was, it must not carry the key.
    assert key not in caplog.text
    assert escaped not in caplog.text


async def test_test_endpoint_exception_log_scrubs_the_key(monkeypatch, caplog):
    """Transport exceptions can quote the key back (SDK auth errors do).
    The failure log must be formatted + scrubbed instead of exc_info."""
    import logging

    from api.routers.model_configs import _do_test

    key = "sk-exception-log-echo"
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _LeakyAsyncClient(key))

    with caplog.at_level(logging.DEBUG):
        result = await _do_test("openai", "gpt-4o", key, None)

    assert result["ok"] is False
    assert result["error"] == "Connection test failed"
    assert key not in caplog.text
    assert "[REDACTED]" in caplog.text
