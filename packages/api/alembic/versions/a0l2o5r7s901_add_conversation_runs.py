"""Create conversation_runs (durable per-turn status)

Revision ID: a0l2o5r7s901
Revises: a1b3c5d7e902
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "a0l2o5r7s901"
down_revision = "a1b3c5d7e902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_runs",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_message_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.UnicodeText(), nullable=True),
        sa.Column("streaming_snapshot", sa.UnicodeText(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_conversation_runs_conversation_started", "conversation_runs", ["conversation_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_conversation_runs_conversation_started", table_name="conversation_runs")
    op.drop_table("conversation_runs")
