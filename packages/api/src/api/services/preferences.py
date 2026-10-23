"""Per-user onboarding state."""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
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
        # Two concurrent first requests for the same user both see no row and
        # both INSERT. A savepoint confines the loser's IntegrityError so the
        # caller's other pending changes survive; the winner's committed row
        # is then read back.
        try:
            async with db.begin_nested():
                row = UserPreference(user_id=user_id)
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await db.get(UserPreference, user_id)
            if row is None:
                raise
    return row


def serialize_preferences(row: UserPreference) -> dict:
    return {
        "onboarding_completed": row.onboarding_completed,
        "onboarding_completed_at": row.onboarding_completed_at,
        "locale": row.locale,
    }
