"""add messages thinking

Revision ID: b3d7e1a4c9f2
Revises: t6f9a3b5d820
Create Date: 2026-09-23

Extended-thinking text shown collapsibly in chat history. Nullable with no
backfill: pre-existing rows keep NULL and the UI hides the block.
"""
import sqlalchemy as sa
from alembic import context, op

revision = "b3d7e1a4c9f2"
down_revision = "t6f9a3b5d820"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline (--sql) mode has no connection to reflect — emit directly.
    if context.is_offline_mode():
        op.add_column("messages", sa.Column("thinking", sa.UnicodeText(), nullable=True))
        return
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("messages")}
    if "thinking" not in existing:
        op.add_column("messages", sa.Column("thinking", sa.UnicodeText(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "thinking")
