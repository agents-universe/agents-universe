"""Backfill conversations.updated_at from the newest message row.

Revision ID: b3d5f7h9k135
Revises: a0l2o5r7s901
Create Date: 2026-08-27

conversations.updated_at existed since the initial schema but no code path
ever wrote it. It now records the last-message time (the conversation list
and the /latest lookup order by COALESCE(updated_at, created_at)), so
existing rows are backfilled from their newest message to make list order
reflect real activity instead of creation time.
"""
from alembic import context, op
from sqlalchemy import text

revision = "b3d5f7h9k135"
down_revision = "a0l2o5r7s901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill needs a live DB - skipped in offline (--sql) mode.
    if context.is_offline_mode():
        return
    # Correlated subquery, portable across all four dialects. Conversations
    # with no messages get NULL (MAX over an empty set) - they keep sorting
    # by created_at via the COALESCE in the API layer.
    op.execute(text(
        """
        UPDATE conversations
        SET updated_at = (
            SELECT MAX(messages.created_at)
            FROM messages
            WHERE messages.conversation_id = conversations.conversation_id
        )
        WHERE updated_at IS NULL
        """
    ))


def downgrade() -> None:
    if context.is_offline_mode():
        return
    # Runtime code writes updated_at now too, and backfilled rows are
    # indistinguishable from runtime ones - reset the whole column, which
    # restores the pre-migration (all-NULL) state.
    op.execute(text("UPDATE conversations SET updated_at = NULL"))
