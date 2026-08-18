"""Add (conversation_id, sequence_num) index on messages.

Revision ID: v6e8g2j5k039
Revises: u5d7f9h1i234
Create Date: 2026-08-13

every turn runs history-load / MAX(sequence_num) queries scoped
to one conversation; without this index they are full table scans on the
largest, unbounded table (SQL Server does not index FKs automatically).
"""
from alembic import op
import sqlalchemy as sa

revision = "v6e8g2j5k039"
down_revision = "u5d7f9h1i234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_conversation_sequence",
        "messages",
        ["conversation_id", "sequence_num"],
    )


def downgrade() -> None:
    # MySQL refuses to drop the last index serving an FK (error 1553) — the
    # initial schema's conversation_id FK (engine-named messages_ibfk_1) is
    # served only by this index. PG/SQL Server/SQLite don't require indexes
    # for FKs and drop or rebuild fine without this guard.
    if op.get_bind().dialect.name == "mysql" and not op.get_context().as_sql:
        for fk in sa.inspect(op.get_bind()).get_foreign_keys("messages"):
            if fk.get("constrained_columns") == ["conversation_id"]:
                op.drop_constraint(fk["name"], "messages", type_="foreignkey")
    op.drop_index("ix_messages_conversation_sequence", table_name="messages")
