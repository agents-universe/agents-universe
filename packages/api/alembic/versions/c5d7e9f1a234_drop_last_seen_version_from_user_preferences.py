"""drop last_seen_version from user_preferences

Revision ID: c5d7e9f1a234
Revises: a3b7d1e2f415
Create Date: 2026-08-29

The "What's new" dialog is removed, so the version marker it read is gone
from the app. onboarding_completed stays (it drives the tour).
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5d7e9f1a234'
down_revision = 'a3b7d1e2f415'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("user_preferences") as batch_op:
            batch_op.drop_column("last_seen_version")
    else:
        op.drop_column("user_preferences", "last_seen_version")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("user_preferences") as batch_op:
            batch_op.add_column(sa.Column("last_seen_version", sa.String(20), nullable=True))
    else:
        op.add_column("user_preferences", sa.Column("last_seen_version", sa.String(20), nullable=True))
