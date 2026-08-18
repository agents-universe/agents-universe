"""add model_id to user_tokens

Revision ID: e5f1c4d8b920
Revises: d4e9b3a2f158
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f1c4d8b920'
down_revision = 'd4e9b3a2f158'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'user_tokens',
        sa.Column('model_id', sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_tokens', 'model_id')
