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
            "locale": None,
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
            "locale": None,
        }


async def test_patch_locale(client, as_user):
    async with as_user("u-pre-locale"):
        resp = await _patch(client, locale="en-US")
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en-US"
        # Survives a later patch of another field and shows up in /api/me.
        await _patch(client, onboarding_completed=True)
        data = await _me(client)
        assert data["preferences"]["locale"] == "en-US"
        assert data["preferences"]["onboarding_completed"] is True


async def test_patch_locale_invalid_value(client, as_user):
    async with as_user("u-pre-locale-bad"):
        resp = await _patch(client, locale="fr-FR")
        assert resp.status_code == 422
        assert (await _me(client))["preferences"]["locale"] is None


async def test_patch_locale_omitted_keeps_existing(client, as_user):
    async with as_user("u-pre-locale-keep"):
        await _patch(client, locale="en-US")
        resp = await _patch(client, onboarding_completed=True)
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en-US"


async def test_patch_reset_onboarding_completed(client, as_user):
    async with as_user("u-pre-reset"):
        await _patch(client, onboarding_completed=True)
        resp = await _patch(client, onboarding_completed=False)
        assert resp.status_code == 200
        body = resp.json()
        assert body["onboarding_completed"] is False
        # Resetting clears the completion timestamp so a re-tour can re-stamp.
        assert body["onboarding_completed_at"] is None


async def test_patch_explicit_null_is_a_noop(client, as_user):
    """null must not be written to the NOT NULL column (500 before the fix)."""
    async with as_user("u-pre-null"):
        await _patch(client, onboarding_completed=True)
        before = (await _me(client))["preferences"]
        resp = await _patch(client, onboarding_completed=None)
        assert resp.status_code == 200
        assert resp.json() == before
        assert (await _me(client))["preferences"] == before


async def test_me_creates_row_once(client, as_user):
    """Two /api/me calls return identical state — the row is created once."""
    async with as_user("u-pre-idempotent"):
        first = (await _me(client))["preferences"]
        second = (await _me(client))["preferences"]
        assert second == first


async def test_concurrent_first_access_recovers_from_the_insert_race(
    db, monkeypatch
):
    """Two first requests both see no row and both INSERT. The loser's
    IntegrityError must not escape: the winner's committed row is read back.

    The stale read is simulated by making the first get() miss the row that
    another request already committed.
    """
    from api.database import AsyncSessionLocal
    from api.models.user import UserPreference
    from api.services.preferences import get_or_create_preferences

    user_id = "u-pre-race"
    async with AsyncSessionLocal() as other:
        other.add(UserPreference(user_id=user_id))
        await other.commit()

    async with AsyncSessionLocal() as session:
        real_get = session.get
        calls = {"n": 0}

        async def stale_first_get(entity, pk, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the racing request's row was not visible yet
            return await real_get(entity, pk, *args, **kwargs)

        monkeypatch.setattr(session, "get", stale_first_get)
        row = await get_or_create_preferences(session, user_id)

        assert row.user_id == user_id
        assert calls["n"] == 2
