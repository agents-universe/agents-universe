"""Per-user onboarding state."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.models.user import UserPreference


async def get_or_create_preferences(db: AsyncSession, user_id: str) -> UserPreference:
    """Fetch the user's preference row, creating it lazily on first access.

    The row is flushed (not committed) so it is persisted by the calling
    route's commit — /api/me, which only reads, relies on the session
    dependency's end-of-request commit to flush it.
    """
    row = await db.get(UserPreference, user_id)
    if row is None:
        row = UserPreference(user_id=user_id)
        db.add(row)
        await db.flush()
    return row


def serialize_preferences(row: UserPreference) -> dict:
    return {
        "onboarding_completed": row.onboarding_completed,
        "onboarding_completed_at": row.onboarding_completed_at,
    }
