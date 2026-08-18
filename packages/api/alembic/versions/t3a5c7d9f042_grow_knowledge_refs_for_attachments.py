"""grow knowledge_refs to UnicodeText for attachment records

Revision ID: t3a5c7d9f042
Revises: s2c5e7d9f031
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa

revision = 't3a5c7d9f042'
down_revision = 's2c5e7d9f031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 附件记录随用户消息持久化在 knowledge_refs JSON 中，2000 字符不够，
    # 扩容为 UnicodeText (SQL Server 上为 NVARCHAR(MAX))。
    # SQLite has no ALTER COLUMN — batch mode rebuilds the table instead.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("messages") as batch_op:
            batch_op.alter_column(
                "knowledge_refs",
                type_=sa.UnicodeText(),
                existing_type=sa.Unicode(2000),
                existing_nullable=True,
            )
        return
    op.alter_column(
        'messages',
        'knowledge_refs',
        type_=sa.UnicodeText(),
        existing_type=sa.Unicode(2000),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'messages',
        'knowledge_refs',
        type_=sa.Unicode(2000),
        existing_type=sa.UnicodeText(),
        existing_nullable=True,
    )
