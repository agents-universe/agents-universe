"""Add knowledge hierarchy fields and load events table.

Revision ID: a7d2e1f4c893
Revises: fb3f72c8b162
Create Date: 2026-06-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7d2e1f4c893"
down_revision = "fb3f72c8b162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add hierarchy columns to knowledge_metadata
    op.add_column("knowledge_metadata", sa.Column("knowledge_level", sa.String(20), server_default="auto", nullable=False))
    op.add_column("knowledge_metadata", sa.Column("parent_slug", sa.String(200), nullable=True))
    op.add_column("knowledge_metadata", sa.Column("children_slugs", sa.String(4000), nullable=True))
    op.add_column("knowledge_metadata", sa.Column("summary", sa.String(500), nullable=True))

    # Indexes for hierarchy queries
    op.create_index("ix_knowledge_level_project", "knowledge_metadata", ["project_id", "knowledge_level"])
    op.create_index("ix_knowledge_parent", "knowledge_metadata", ["parent_slug"])

    # GETUTCDATE() is T-SQL-only — other dialects rely on the ORM's
    # client-side now_utc() default.
    created_at_default = sa.func.getutcdate() if op.get_bind().dialect.name == "mssql" else None
    op.create_table(
        "knowledge_load_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("knowledge_id", sa.String(36), sa.ForeignKey("knowledge_metadata.knowledge_id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.conversation_id"), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(50), nullable=True),
        sa.Column("turn_number", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=created_at_default),
    )


def downgrade() -> None:
    op.drop_table("knowledge_load_events")
    # MySQL requires every FK to be served by an index and refuses to drop
    # one it needs (error 1553). The initial schema's unnamed project_id FK
    # (engine-named knowledge_metadata_ibfk_1) is served by
    # ix_knowledge_level_project — resolve the generated name and drop the
    # constraint first. PG/SQL Server/SQLite don't require indexes for FKs.
    if (
        op.get_bind().dialect.name == "mysql"
        and not op.get_context().as_sql
    ):
        for fk in sa.inspect(op.get_bind()).get_foreign_keys("knowledge_metadata"):
            if fk.get("constrained_columns") == ["project_id"]:
                op.drop_constraint(fk["name"], "knowledge_metadata", type_="foreignkey")
    op.drop_index("ix_knowledge_parent", table_name="knowledge_metadata")
    op.drop_index("ix_knowledge_level_project", table_name="knowledge_metadata")
    op.drop_column("knowledge_metadata", "summary")
    op.drop_column("knowledge_metadata", "children_slugs")
    op.drop_column("knowledge_metadata", "parent_slug")
    op.drop_column("knowledge_metadata", "knowledge_level")
