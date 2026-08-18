"""Enforce one episodic summary per conversation.

Revision ID: u5d7f9h1i234
Revises: t3a5c7d9f042
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy import text

revision = "u5d7f9h1i234"
down_revision = "t3a5c7d9f042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe legacy rows first — a unique constraint cannot be created while
    # two summaries exist for the same conversation. Keep the newest row per
    # conversation (tie-broken by episode_id, which is a UUID string).
    # A correlated `DELETE ... WHERE EXISTS (SELECT ... FROM episodic_memories ...)`
    # reads the target table in a subquery, which MySQL rejects (error 1093) —
    # a derived-table ROW_NUMBER join works on all four dialects (SQL Server,
    # SQLite 3.25+, PostgreSQL, MySQL 8.0). episode_id is a non-null PK, so
    # NOT IN is NULL-safe.
    # Dedupe needs a live DB — skipped in offline (--sql) mode.
    if not context.is_offline_mode():
        conn = op.get_bind()
        conn.execute(text(
            "DELETE FROM episodic_memories WHERE episode_id NOT IN ("
            "  SELECT episode_id FROM ("
            "    SELECT episode_id, ROW_NUMBER() OVER ("
            "      PARTITION BY conversation_id ORDER BY created_at DESC, episode_id DESC"
            "    ) AS rn FROM episodic_memories"
            "  ) ranked WHERE rn = 1"
            ")"
        ))
    if op.get_bind().dialect.name == "sqlite":
        # SQLite has no ALTER TABLE ADD CONSTRAINT — a unique index gives the
        # same uniqueness semantics (mirrors j2e6g3c5f784).
        op.create_index(
            "uq_episodic_memories_conversation_id",
            "episodic_memories",
            ["conversation_id"],
            unique=True,
        )
    else:
        op.create_unique_constraint("uq_episodic_memories_conversation_id", "episodic_memories", ["conversation_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        # MySQL's unique constraint IS the index (no DROP CONSTRAINT for
        # unique), and it refuses to drop the last index serving an FK
        # (1553) — the initial schema's conversation_id FK is served only
        # by this unique index, so drop the generated FK name first.
        if not op.get_context().as_sql:
            for fk in sa.inspect(op.get_bind()).get_foreign_keys("episodic_memories"):
                if fk.get("constrained_columns") == ["conversation_id"]:
                    op.drop_constraint(fk["name"], "episodic_memories", type_="foreignkey")
        op.drop_index("uq_episodic_memories_conversation_id", table_name="episodic_memories")
    elif op.get_bind().dialect.name == "sqlite":
        op.drop_index("uq_episodic_memories_conversation_id", table_name="episodic_memories")
    else:
        op.drop_constraint("uq_episodic_memories_conversation_id", "episodic_memories", type_="unique")
