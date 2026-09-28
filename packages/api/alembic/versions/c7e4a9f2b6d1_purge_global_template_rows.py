"""Purge global _template/* knowledge rows seeded by the buggy global index.

Revision ID: c7e4a9f2b6d1
Revises: e4f6a8c1d273
Create Date: 2026-09-28

The deployment entrypoint indexes the whole knowledge/ tree globally, and
the indexer used to walk _template/ as well: every template file became a
project_id NULL row (slug "_template/<file>") whose title duplicates the
copy each project receives at creation. Those rows polluted every project's
knowledge panel and agent context, and could never be deleted from a project
(delete_one matches an exact project_id). The indexer now skips _template/
for global scope, so this is a one-time cleanup of the historical residue.
"""
from alembic import context, op
from sqlalchemy import bindparam, text

revision = "c7e4a9f2b6d1"
down_revision = "e4f6a8c1d273"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Data cleanup needs a live DB - skipped in offline (--sql) mode.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    rows = bind.execute(text(
        "SELECT knowledge_id, slug FROM knowledge_metadata WHERE project_id IS NULL"
    )).fetchall()
    # Filter in Python, not SQL LIKE: "_" is a LIKE metacharacter ("_template/%"
    # would also match "xtemplate/...") and ESCAPE clauses differ per dialect
    # (MySQL backslash string literals). Global rows are few, so fetching them
    # all and matching with str.startswith is exact on all four dialects.
    kids = [str(row[0]) for row in rows if str(row[1]).startswith("_template/")]
    if not kids:
        return
    # Children first: knowledge_load_events.knowledge_id has no ON DELETE
    # clause (a plain FK would abort the metadata delete), and SQLite does not
    # enforce knowledge_versions' DB-level CASCADE - same order as
    # agent_core.knowledge.index._hard_delete_knowledge_id.
    for table in ("knowledge_load_events", "knowledge_versions", "knowledge_metadata"):
        bind.execute(
            text(f"DELETE FROM {table} WHERE knowledge_id IN :kids").bindparams(
                bindparam("kids", expanding=True)
            ),
            {"kids": kids},
        )


def downgrade() -> None:
    # Purged rows are unrecoverable - they were only ever produced by running
    # the old global indexer over knowledge/_template/, which no longer does.
    if context.is_offline_mode():
        return
