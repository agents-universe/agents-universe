"""add user_preferences table

Revision ID: d9a5c2e7b123
Revises: z8k1n4p6q780
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9a5c2e7b123'
down_revision = 'a3f7k2m9p1q5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_preferences',
        sa.Column('user_id', sa.String(100), primary_key=True),
        sa.Column('onboarding_completed', sa.Boolean, nullable=False),
        sa.Column('onboarding_completed_at', sa.DateTime, nullable=True),
        sa.Column('last_seen_version', sa.String(20), nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('updated_at', sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table('user_preferences')
