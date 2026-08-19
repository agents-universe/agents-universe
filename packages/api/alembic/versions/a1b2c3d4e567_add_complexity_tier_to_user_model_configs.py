"""add complexity_tier to user_model_configs

Revision ID: a1b2c3d4e567
Revises: z8k1n4p6q780
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e567'
down_revision = 'z8k1n4p6q780'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no server_default: assignment is app-side (inferred on create,
    # editable in Settings). Keeps the DDL portable across all four dialects.
    op.add_column(
        'user_model_configs',
        sa.Column('complexity_tier', sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_model_configs', 'complexity_tier')
