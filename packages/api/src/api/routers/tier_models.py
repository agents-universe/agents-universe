"""User model configuration — one row per LLM provider per user."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.user import UserApiKey, UserToken, UserTierModel

router = APIRouter()

VALID_PROVIDERS = {"anthropic", "openai", "azure_openai", "google_gemini"}


class ModelConfigUpsert(BaseModel):
    # unbounded model_id overran the model_id column String(200)
    # with a DataError on MSSQL — bound to the column width.
    model_id: str = Field(..., max_length=200)


@router.get("")
async def list_model_configs(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    user_id = current_user.user_id

    result = await db.execute(
        select(UserTierModel).where(UserTierModel.user_id == user_id)
    )
    rows = result.scalars().all()

    # Collect providers that have an API key (from either table)
    keys_result = await db.execute(
        select(UserApiKey.provider).where(UserApiKey.user_id == user_id)
    )
    providers_with_key = {r[0] for r in keys_result.fetchall()}

    legacy_result = await db.execute(
        select(UserToken.service_key).where(UserToken.user_id == user_id)
    )
    for r in legacy_result.fetchall():
        provider = r[0].split(":", 1)[0]
        if provider in VALID_PROVIDERS:
            providers_with_key.add(provider)

    return [
        {"provider": r.provider, "model_id": r.model_id}
        for r in rows
        if r.provider in providers_with_key
    ]


@router.put("/{provider}")
async def upsert_model_config(
    provider: str,
    body: ModelConfigUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}. Must be one of {sorted(VALID_PROVIDERS)}")
    if not body.model_id.strip():
        raise HTTPException(status_code=400, detail="model_id cannot be empty")

    user_id = current_user.user_id
    result = await db.execute(
        select(UserTierModel).where(
            UserTierModel.user_id == user_id,
            UserTierModel.tier == provider,
        ).with_for_update()
    )
    row = result.scalar_one_or_none()

    if row:
        row.provider = provider
        row.model_id = body.model_id.strip()
    else:
        row = UserTierModel(
            user_id=user_id,
            tier=provider,
            provider=provider,
            model_id=body.model_id.strip(),
        )
        db.add(row)

    try:
        await db.commit()
    except IntegrityError:
        # Concurrent PUTs for a missing row both pass the None branch above
        # (with_for_update() locks only existing rows) — the loser violates
        # uq_user_tier_model on commit.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Model config already exists (concurrent creation)")
    return {"provider": provider, "model_id": body.model_id.strip()}


@router.delete("/{provider}")
async def delete_model_config(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserTierModel).where(
            UserTierModel.user_id == current_user.user_id,
            UserTierModel.tier == provider,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Model config not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": provider}
