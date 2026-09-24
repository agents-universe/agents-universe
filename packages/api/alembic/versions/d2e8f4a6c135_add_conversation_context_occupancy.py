"""add conversation context occupancy columns

Revision ID: d2e8f4a6c135
Revises: c9f2d4a8b176
Create Date: 2026-09-24

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2e8f4a6c135'
down_revision = 'c9f2d4a8b176'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable plain add_column — dialect-safe across all four dialects.
    op.add_column("conversations", sa.Column("context_tokens", sa.Integer(), nullable=True))
    op.add_column("conversations", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "context_window")
    op.drop_column("conversations", "context_tokens")
