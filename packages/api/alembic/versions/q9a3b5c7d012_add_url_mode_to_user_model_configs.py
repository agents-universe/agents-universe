"""add url_mode to user_model_configs

Revision ID: q9a3b5c7d012
Revises: p8r2s4t6u890
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'q9a3b5c7d012'
down_revision = 'p8r2s4t6u890'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'user_model_configs',
        sa.Column('url_mode', sa.String(20), nullable=False, server_default='base_url'),
    )


def downgrade() -> None:
    op.drop_column('user_model_configs', 'url_mode')
