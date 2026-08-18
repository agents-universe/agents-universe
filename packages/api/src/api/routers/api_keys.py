"""LLM provider API key management."""
from __future__ import annotations

import logging
import traceback

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.user import UserApiKey
from api.services.token_vault import decrypt_or_none, encrypt, key_hint

router = APIRouter()

VALID_PROVIDERS = {"anthropic", "openai", "azure_openai", "google_gemini"}


class ApiKeyUpsert(BaseModel):
    # None ⇒ keep the existing key ("__keep__" accepted as deprecated alias)
    # bounded to the encrypted_value column (String(4000)): AES-GCM ciphertext
    # is plaintext+28 bytes and base64 expands it ~4/3, so 2800 chars store as
    # ~3772 chars — longer values raise DataError on MSSQL. Mirrors
    # TokenUpsert.value (tokens.py).
    value: str | None = Field(None, max_length=2800)


@router.get("")
async def list_api_keys(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserApiKey).where(UserApiKey.user_id == current_user.user_id)
    )
    keys = result.scalars().all()
    return [
        {
            "provider": k.provider,
            "key_hint": k.key_hint,
            # Kept for legacy clients; no system-level endpoint is injected.
            "base_url": k.base_url,
        }
        for k in keys
    ]


@router.get("/base-urls")
async def get_base_urls():
    """Deprecated compatibility endpoint; endpoints are configured per model."""
    return {}


@router.put("/{provider}")
async def upsert_api_key(
    provider: str,
    body: ApiKeyUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider!r}")

    user_id = current_user.user_id
    result = await db.execute(
        select(UserApiKey).where(
            UserApiKey.user_id == user_id,
            UserApiKey.provider == provider,
        ).with_for_update()
    )
    row = result.scalar_one_or_none()

    # value=None (or legacy "__keep__") ⇒ keep the existing key
    keep_existing = body.value in (None, "__keep__")
    if keep_existing:
        if row is None:
            raise HTTPException(status_code=400, detail="No existing key to keep")
        encrypted = row.encrypted_value
        hint = row.key_hint
    else:
        encrypted = encrypt(body.value, user_id)
        hint = key_hint(body.value)

    if row:
        row.encrypted_value = encrypted
        row.key_hint = hint
    else:
        row = UserApiKey(
            user_id=user_id,
            provider=provider,
            encrypted_value=encrypted,
            key_hint=hint,
        )
        db.add(row)

    try:
        await db.commit()
    except IntegrityError:
        # Concurrent PUTs for a missing row both pass the None branch above
        # (with_for_update() locks only existing rows) — the loser violates
        # uq_user_api_key_provider on commit.
        await db.rollback()
        raise HTTPException(status_code=409, detail="API key already exists (concurrent creation)")
    return {"provider": provider, "key_hint": hint}


@router.delete("/{provider}")
async def delete_api_key(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserApiKey).where(
            UserApiKey.user_id == current_user.user_id,
            UserApiKey.provider == provider,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": provider}


@router.post("/{provider}/test")
async def test_api_key(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserApiKey).where(
            UserApiKey.user_id == current_user.user_id,
            UserApiKey.provider == provider,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="API key not configured")

    if provider == "azure_openai":
        return {
            "ok": False,
            "provider": provider,
            "error": "Azure OpenAI now requires a per-model endpoint. Migrate this key in Settings → AI Models.",
        }

    plain = decrypt_or_none(row.encrypted_value, current_user.user_id)
    if plain is None:
        return {"ok": False, "provider": provider, "error": "Stored API key is corrupted — delete and re-save it"}

    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=plain)
            await client.models.list()
            return {"ok": True, "provider": provider}
        elif provider == "openai":
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=plain)
            await client.models.list()
            return {"ok": True, "provider": provider}
        else:
            return {"ok": True, "provider": provider, "note": "connectivity test not implemented for this provider"}
    except Exception as exc:
        logger.error("API key test failed for %s: %s", provider, traceback.format_exc())
        return {"ok": False, "provider": provider, "error": str(exc) or "Token test failed"}
