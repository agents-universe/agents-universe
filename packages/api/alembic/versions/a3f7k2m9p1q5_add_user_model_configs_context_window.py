"""add user_model_configs.context_window

Revision ID: a3f7k2m9p1q5
Revises: z8k1n4p6q780
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3f7k2m9p1q5'
down_revision = 'a1b2c3d4e567'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable per-config override; NULL = name-matched default at runtime.
    op.add_column("user_model_configs", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_model_configs", "context_window")
