"""Per-user preferences — onboarding tour state and UI locale."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.services.preferences import get_or_create_preferences, serialize_preferences

router = APIRouter()


class PreferencesUpdate(BaseModel):
    onboarding_completed: bool | None = None
    locale: Literal["zh-CN", "en-US"] | None = None


@router.patch("")
async def update_preferences(
    body: PreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    row = await get_or_create_preferences(db, current_user.user_id)
    # Explicit null means "no change" (the field is optional, and the column is
    # NOT NULL — writing None here would fail the commit with a 500). Only a
    # real boolean changes state.
    if body.onboarding_completed is not None:
        row.onboarding_completed = body.onboarding_completed
        if body.onboarding_completed:
            if row.onboarding_completed_at is None:
                row.onboarding_completed_at = datetime.now(timezone.utc)
        else:
            # Reset clears the stamp so a later re-tour re-stamps fresh.
            row.onboarding_completed_at = None
    # The column is nullable (None = never chosen); an omitted field must not
    # wipe an existing choice, so only a present value is written.
    if body.locale is not None:
        row.locale = body.locale
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return serialize_preferences(row)
