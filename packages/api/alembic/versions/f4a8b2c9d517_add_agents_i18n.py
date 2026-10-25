"""add i18n locale-map column to agents

Revision ID: f4a8b2c9d517
Revises: e3f8a1c6d294
Create Date: 2026-10-25

JSON object keyed by field name, each holding a {locale: value} map for the
non-canonical locales of display_name/description/placeholder/starter_prompts:
{"display_name": {"en-US": "Tech Lead"}, ...}. NULL when the definition file
uses plain scalars (every pre-i18n file).
"""
from alembic import op
import sqlalchemy as sa

revision = 'f4a8b2c9d517'
down_revision = 'e3f8a1c6d294'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("i18n", sa.UnicodeText(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "i18n")
