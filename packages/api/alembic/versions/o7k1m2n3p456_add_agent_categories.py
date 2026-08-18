"""add agent categories

Revision ID: o7k1m2n3p456
Revises: n6j0k7g9h128
"""
from alembic import op
import sqlalchemy as sa

revision = "o7k1m2n3p456"
down_revision = "n6j0k7g9h128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("category", sa.String(100), nullable=True))
    op.execute("UPDATE agents SET category = 'agile-development' WHERE category IS NULL")
    if op.get_bind().dialect.name == "mssql":
        op.execute("ALTER TABLE agents ALTER COLUMN category VARCHAR(100) NOT NULL")
        op.execute(
            "ALTER TABLE agents ADD CONSTRAINT DF_agents_category DEFAULT 'agile-development' FOR category"
        )
    else:
        # MySQL has no named DEFAULT constraint objects — op.alter_column
        # compiles to the right DDL on each dialect (MySQL renders MODIFY
        # COLUMN inline). SQLite has no ALTER COLUMN — batch rebuild instead.
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("agents") as batch_op:
                batch_op.alter_column(
                    "category",
                    existing_type=sa.String(100),
                    existing_nullable=True,
                    nullable=False,
                    server_default="agile-development",
                )
        else:
            op.alter_column(
                "agents",
                "category",
                existing_type=sa.String(100),
                existing_nullable=True,
                nullable=False,
                server_default="agile-development",
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "mssql":
        op.execute("ALTER TABLE agents DROP CONSTRAINT DF_agents_category")
    else:
        # MySQL 8.0.19+ DROP CONSTRAINT covers FK/CHECK only, never defaults.
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("agents") as batch_op:
                batch_op.alter_column(
                    "category",
                    existing_type=sa.String(100),
                    nullable=True,
                    server_default=None,
                )
        else:
            op.alter_column(
                "agents",
                "category",
                existing_type=sa.String(100),
                nullable=True,
                server_default=None,
            )
    op.drop_column("agents", "category")
