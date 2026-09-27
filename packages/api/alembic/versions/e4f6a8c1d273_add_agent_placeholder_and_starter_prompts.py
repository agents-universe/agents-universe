"""add agent placeholder and starter prompts

Revision ID: e4f6a8c1d273
Revises: b5d8e2f7a9c3
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'e4f6a8c1d273'
down_revision = 'b5d8e2f7a9c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'agents',
        sa.Column('placeholder', sa.Unicode(500), nullable=True),
    )
    op.add_column(
        'agents',
        sa.Column('starter_prompts', sa.UnicodeText(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('agents', 'starter_prompts')
    op.drop_column('agents', 'placeholder')
