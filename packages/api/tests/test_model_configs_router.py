"""Tests for /api/model-configs CRUD + complexity_tier inference."""
from __future__ import annotations

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
