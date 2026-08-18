"""Create durable task execution timeline events.

Revision ID: n6j0k7g9h128
Revises: m5h9j6f8k017
"""
from alembic import op
import sqlalchemy as sa

revision = "n6j0k7g9h128"
down_revision = "m5h9j6f8k017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MySQL rejects literal DEFAULTs on TEXT columns (error 1101); the ORM
    # supplies payload via its client-side default, so skip the server
    # default there. NVARCHAR(MAX)/PG TEXT/SQLite all allow it.
    payload_default = "{}" if op.get_bind().dialect.name != "mysql" else None
    op.create_table(
        "task_events",
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.UnicodeText(), nullable=False, server_default=payload_default),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.conversation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_task_event_conversation_sequence"),
    )
    op.create_index("ix_task_events_conversation_id", "task_events", ["conversation_id"])
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_index("ix_task_events_conversation_id", table_name="task_events")
    op.drop_table("task_events")
