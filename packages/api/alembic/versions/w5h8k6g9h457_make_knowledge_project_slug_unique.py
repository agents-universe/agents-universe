"""Make (project_id, slug) unique on knowledge_metadata.

Revision ID: w5h8k6g9h457
Revises: l4g8i5e7h906
Create Date: 2026-08-13

Concurrent index_directory/reindex_one runs could INSERT duplicate
(project_id, slug) rows (the ix_knowledge_project_slug index was not
unique), after which scalar_one_or_none() reads raise
MultipleResultsFound. Deduplicate existing rows (keep the newest), then
make the index unique.
"""
from alembic import context, op
from sqlalchemy import text

revision = "w5h8k6g9h457"
down_revision = "v6e8g2j5k039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dedupe needs a live DB — skipped in offline (--sql) mode.
    if not context.is_offline_mode():
        bind = op.get_bind()

        # Keep the newest row per (project_id, slug) — window function works on
        # both SQL Server and SQLite (3.25+). NULL project_id groups together,
        # which is correct: global knowledge keys on slug alone.
        rows = bind.execute(text(
            """
            SELECT knowledge_id
            FROM (
                SELECT knowledge_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY project_id, slug
                           ORDER BY updated_at DESC, knowledge_id DESC
                       ) AS rn
                FROM knowledge_metadata
            ) ranked
            WHERE rn > 1
            """
        )).fetchall()

        for (kid,) in rows:
            bind.execute(
                text("DELETE FROM knowledge_load_events WHERE knowledge_id = :kid"),
                {"kid": kid},
            )
            bind.execute(
                text("DELETE FROM knowledge_versions WHERE knowledge_id = :kid"),
                {"kid": kid},
            )
            bind.execute(
                text("DELETE FROM knowledge_metadata WHERE knowledge_id = :kid"),
                {"kid": kid},
            )

    op.drop_index("ix_knowledge_project_slug", table_name="knowledge_metadata")
    op.create_index(
        "ix_knowledge_project_slug",
        "knowledge_metadata",
        ["project_id", "slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_project_slug", table_name="knowledge_metadata")
    op.create_index(
        "ix_knowledge_project_slug",
        "knowledge_metadata",
        ["project_id", "slug"],
    )
