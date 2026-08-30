"""Tests for /api/me preferences + PATCH /api/preferences."""
from __future__ import annotations


async def _me(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    return resp.json()


async def _patch(client, **body):
    return await client.patch("/api/preferences", json=body)


async def test_me_returns_default_preferences(client, as_user):
    async with as_user("u-pre-default"):
        data = await _me(client)
        assert data["user_id"] == "u-pre-default"
        assert data["preferences"] == {
            "onboarding_completed": False,
            "onboarding_completed_at": None,
        }


async def test_patch_onboarding_completed(client, as_user):
    async with as_user("u-pre-complete"):
        resp = await _patch(client, onboarding_completed=True)
        assert resp.status_code == 200
        body = resp.json()
        assert body["onboarding_completed"] is True
        assert body["onboarding_completed_at"] is not None

        data = await _me(client)
        assert data["preferences"]["onboarding_completed"] is True


async def test_patch_partial_does_not_clear_other_field(client, as_user):
    async with as_user("u-pre-partial"):
        # Completing stamps the timestamp; re-patching the same field without
        # changing it must not clear the existing stamp.
        await _patch(client, onboarding_completed=True)
        first = (await _me(client))["preferences"]
        await _patch(client, onboarding_completed=True)
        data = await _me(client)
        prefs = data["preferences"]
        assert prefs["onboarding_completed"] is True
        assert prefs["onboarding_completed_at"] == first["onboarding_completed_at"]


async def test_patch_empty_body_noop(client, as_user):
    async with as_user("u-pre-empty"):
        before = (await _me(client))["preferences"]
        resp = await _patch(client)
        assert resp.status_code == 200
        assert resp.json() == before


async def test_preferences_scoped_per_user(client, as_user):
    async with as_user("u-pre-a"):
        await _patch(client, onboarding_completed=True)
    async with as_user("u-pre-b"):
        data = await _me(client)
        assert data["preferences"] == {
            "onboarding_completed": False,
            "onboarding_completed_at": None,
        }


async def test_patch_reset_onboarding_completed(client, as_user):
    async with as_user("u-pre-reset"):
        await _patch(client, onboarding_completed=True)
        resp = await _patch(client, onboarding_completed=False)
        assert resp.status_code == 200
        body = resp.json()
        assert body["onboarding_completed"] is False
        # Resetting clears the completion timestamp so a re-tour can re-stamp.
        assert body["onboarding_completed_at"] is None


async def test_me_creates_row_once(client, as_user):
    """Two /api/me calls return identical state — the row is created once."""
    async with as_user("u-pre-idempotent"):
        first = (await _me(client))["preferences"]
        second = (await _me(client))["preferences"]
        assert second == first
