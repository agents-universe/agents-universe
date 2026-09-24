"""Context occupancy columns: restore fields + occupancy-based percent.

The lifetime billing ledger (tokens_used) may far exceed token_budget by
design — percent must reflect the meter's context occupancy instead."""
from __future__ import annotations

import pytest

from api.models.conversation import Conversation


async def _seed(db, make_project, **kw):
    project = await make_project()
    conv = Conversation(user_id="test-user", project_id=project.project_id, title="t", **kw)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return project, conv


@pytest.mark.asyncio
async def test_token_usage_percent_uses_occupancy(client, db, make_project):
    _, conv = await _seed(
        db,
        make_project,
        tokens_used=500_000,
        token_budget=128_000,
        context_tokens=45_000,
        context_window=200_000,
    )
    resp = await client.get(f"/api/conversations/{conv.conversation_id}/token-usage")
    assert resp.status_code == 200
    data = resp.json()
    # Occupancy-based: 45k / 200k = 22.5, NOT the lifetime blowout (390%).
    assert data["percent"] == 22.5
    assert data["context_tokens"] == 45_000
    assert data["context_window"] == 200_000
    # Billing figures kept for compat.
    assert data["tokens_used"] == 500_000
    assert data["token_budget"] == 128_000


@pytest.mark.asyncio
async def test_token_usage_falls_back_to_budget_window(client, db, make_project):
    # Legacy row: occupancy/window never written — percent falls back to
    # token_budget as denominator and 0 occupancy.
    _, conv = await _seed(db, make_project, tokens_used=10_000, token_budget=128_000)
    resp = await client.get(f"/api/conversations/{conv.conversation_id}/token-usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["context_tokens"] is None
    assert data["context_window"] is None
    assert data["percent"] == 0


@pytest.mark.asyncio
async def test_latest_conversation_returns_occupancy_fields(client, db, make_project):
    project, conv = await _seed(
        db,
        make_project,
        tokens_used=30_000,
        token_budget=128_000,
        context_tokens=18_400,
        context_window=200_000,
    )
    resp = await client.get(f"/api/projects/{project.project_id}/conversations/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation_id"] == conv.conversation_id
    assert data["context_tokens"] == 18_400
    assert data["context_window"] == 200_000
    assert data["tokens_used"] == 30_000


@pytest.mark.asyncio
async def test_conversation_list_returns_occupancy_fields(client, db, make_project):
    project, conv = await _seed(
        db,
        make_project,
        context_tokens=9_000,
        context_window=128_000,
    )
    resp = await client.get(f"/api/projects/{project.project_id}/conversations")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["conversation_id"] == conv.conversation_id)
    assert row["context_tokens"] == 9_000
    assert row["context_window"] == 128_000
