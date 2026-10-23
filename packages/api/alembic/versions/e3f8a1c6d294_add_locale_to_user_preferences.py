"""add locale to user_preferences

Revision ID: e3f8a1c6d294
Revises: b4d7e2a9c518
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'e3f8a1c6d294'
down_revision = 'b4d7e2a9c518'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_preferences", sa.Column("locale", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("user_preferences", "locale")
