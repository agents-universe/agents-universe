"""add base_url to user_tokens

Revision ID: c3f8a2b1d047
Revises: a7d2e1f4c893
Create Date: 2026-06-21
"""
from alembic import context, op
import sqlalchemy as sa

revision = 'c3f8a2b1d047'
down_revision = 'a7d2e1f4c893'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline (--sql) mode has no live connection to reflect — treat as the
    # fresh-DB case (column missing).
    if context.is_offline_mode():
        op.add_column('user_tokens', sa.Column('base_url', sa.String(500), nullable=True))
        return
    # Reflection instead of INFORMATION_SCHEMA — the latter has no SQLite
    # equivalent, so the old query crashed there.
    bind = op.get_bind()
    cols = [c["name"] for c in sa.inspect(bind).get_columns("user_tokens")]
    if "base_url" not in cols:
        op.add_column('user_tokens', sa.Column('base_url', sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column('user_tokens', 'base_url')
