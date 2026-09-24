"""Migrated schema must match what the models declare.

The session schema is built by the real alembic chain (see conftest), so
model-level constraints and column widths that no migration ever applied
are invisible to ORM tests until behavior depends on them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError


async def test_user_tokens_enforce_unique_user_service(db):
    """(user_id, service_key) must be unique in the migrated schema.

    routers/tokens.upsert_token races concurrent PUTs on the IntegrityError
    from uq_user_token_service (the loser commits second) — but no migration
    ever created that constraint, so on a fresh DB both inserts succeed and
    every later scalar_one_or_none() on that pair raises MultipleResultsFound.
    """
    from api.models.user import UserToken

    db.add(UserToken(user_id="u-tok", service_key="git", encrypted_value="v1"))
    await db.commit()

    db.add(UserToken(user_id="u-tok", service_key="git", encrypted_value="v2"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_tier_column_matches_model_width(db):
    """user_tier_models.tier is String(50) in the model but was migrated as
    String(10) — PUT /api/tier-models/{azure_openai|google_gemini} writes a
    12-13 char tier name, which overflows VARCHAR(10) on MSSQL/PG/MySQL
    (SQLite never enforces the length, which is why only the strict dialects
    would 500).
    """
    # run_sync: aiosqlite has no usable sync DBAPI, so inspecting the engine
    # directly from a sync test raises MissingGreenlet — and run_sync hands
    # the fn a sync Session, so grab its connection for the inspector.
    cols = await db.run_sync(
        lambda s: {
            c["name"]: c for c in inspect(s.connection()).get_columns("user_tier_models")
        }
    )
    assert cols["tier"]["type"].length == 50
