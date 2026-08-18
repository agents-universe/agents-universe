"""Remove workspace layer — drop workspace_id from projects, drop workspace tables.

Revision ID: j2e6g3c5f784
Revises: i1d5f2b4e673
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op

revision = "j2e6g3c5f784"
down_revision = "i1d5f2b4e673"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial schema used unnamed constraints. Resolve the generated SQL
    # Server FK name before dropping the workspace column instead of assuming
    # a constraint name that was never created. SQLite test databases already
    # have FK enforcement disabled by default, so only use its supported DDL.
    if op.get_bind().dialect.name == "mssql":
        op.execute("""
            DECLARE @fk sysname;
            SELECT @fk = fk.name
            FROM sys.foreign_keys AS fk
            JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.columns AS c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
            WHERE fk.parent_object_id = OBJECT_ID(N'projects') AND c.name = N'workspace_id';
            IF @fk IS NOT NULL EXEC(N'ALTER TABLE projects DROP CONSTRAINT [' + @fk + N']');
        """)
        op.drop_column("projects", "workspace_id")
    else:
        # PostgreSQL drops the column's own FK constraint automatically;
        # MySQL refuses to drop an FK-referenced column (error 1828) and
        # names the unnamed initial-schema constraint itself (projects_ibfk_2),
        # so resolve the generated name via reflection. SQLite's batch mode
        # rebuilds the table, carrying no constraints at all.
        if (
            op.get_bind().dialect.name == "mysql"
            and not op.get_context().as_sql
        ):
            for fk in sa.inspect(op.get_bind()).get_foreign_keys("projects"):
                if fk.get("constrained_columns") == ["workspace_id"]:
                    op.drop_constraint(fk["name"], "projects", type_="foreignkey")
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_column("workspace_id")

    # 3. Add global unique constraint on slug
    if op.get_bind().dialect.name == "sqlite":
        op.create_index("uq_project_slug", "projects", ["slug"], unique=True)
    else:
        op.create_unique_constraint("uq_project_slug", "projects", ["slug"])

    # 4. Drop workspace_members table (depends on workspaces)
    op.drop_table("workspace_members")

    # 5. Drop workspaces table
    op.drop_table("workspaces")


def downgrade() -> None:
    # Recreate workspaces table
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.Unicode(length=255), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.UniqueConstraint("slug"),
    )

    # Recreate workspace_members table
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="member"),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.workspace_id"]),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )

    # Drop global slug constraint, re-add workspace_id column and composite constraint
    op.drop_constraint("uq_project_slug", "projects", type_="unique")
    op.add_column("projects", sa.Column("workspace_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(None, "projects", "workspaces", ["workspace_id"], ["workspace_id"])
    op.create_unique_constraint("uq_project_slug_per_workspace", "projects", ["workspace_id", "slug"])
