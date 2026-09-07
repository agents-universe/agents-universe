"""add publish scope columns to conversations

Revision ID: e2b9d4f6a812
Revises: c5d7e9f1a234
Create Date: 2026-09-07

Per-(publish, viewer) and per-(publish, thread) conversation isolation for
the publish surfaces. Legacy source='publish' rows keep these NULL and are
deliberately left unreachable — no backfill, every publish/viewer/thread
starts a fresh conversation (see services/publish.py).
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2b9d4f6a812'
down_revision = 'c5d7e9f1a234'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New scope columns, all nullable — no backfill, so no
    # context.is_offline_mode() guard is needed.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.add_column(sa.Column("publish_id", sa.String(36), nullable=True))
            batch_op.add_column(sa.Column("viewer_id", sa.String(100), nullable=True))
            batch_op.add_column(sa.Column("thread_id", sa.String(100), nullable=True))
    else:
        op.add_column("conversations", sa.Column("publish_id", sa.String(36), nullable=True))
        op.add_column("conversations", sa.Column("viewer_id", sa.String(100), nullable=True))
        op.add_column("conversations", sa.Column("thread_id", sa.String(100), nullable=True))
    op.create_index("ix_conversations_publish_id", "conversations", ["publish_id"])


def downgrade() -> None:
    op.drop_index("ix_conversations_publish_id", table_name="conversations")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.drop_column("thread_id")
            batch_op.drop_column("viewer_id")
            batch_op.drop_column("publish_id")
    else:
        op.drop_column("conversations", "thread_id")
        op.drop_column("conversations", "viewer_id")
        op.drop_column("conversations", "publish_id")
