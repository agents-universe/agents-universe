"""Per-user preferences — onboarding tour state."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.services.preferences import get_or_create_preferences, serialize_preferences

router = APIRouter()


class PreferencesUpdate(BaseModel):
    onboarding_completed: bool | None = None


@router.patch("")
async def update_preferences(
    body: PreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    row = await get_or_create_preferences(db, current_user.user_id)
    if "onboarding_completed" in body.model_fields_set:
        row.onboarding_completed = body.onboarding_completed
        if body.onboarding_completed and row.onboarding_completed_at is None:
            row.onboarding_completed_at = datetime.now(timezone.utc)
        elif not body.onboarding_completed:
            # Reset clears the stamp so a later re-tour re-stamps fresh.
            row.onboarding_completed_at = None
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return serialize_preferences(row)
