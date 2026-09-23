"""add user_model_configs.thinking_enabled, reasoning_effort

Revision ID: c9f2d4a8b176
Revises: b3d7e1a4c9f2
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'c9f2d4a8b176'
down_revision = 'b3d7e1a4c9f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable per-config overrides; NULL keeps the pre-existing behavior
    # (env AGENT_EXTENDED_THINKING for thinking, no reasoning_effort sent).
    op.add_column("user_model_configs", sa.Column("thinking_enabled", sa.Boolean(), nullable=True))
    op.add_column("user_model_configs", sa.Column("reasoning_effort", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("user_model_configs", "reasoning_effort")
    op.drop_column("user_model_configs", "thinking_enabled")
