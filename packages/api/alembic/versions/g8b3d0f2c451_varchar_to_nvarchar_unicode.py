"""varchar to nvarchar for unicode support

Revision ID: g8b3d0f2c451
Revises: f7a2c9e1b340
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'g8b3d0f2c451'
down_revision = 'f7a2c9e1b340'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NVARCHAR conversion is a SQL Server concern only — on PostgreSQL, MySQL
    # and SQLite, Unicode(n) and String(n) compile to the same type
    # (VARCHAR(n), TEXT), so the entire migration is a no-op there. Guarding
    # also avoids SQLite's missing ALTER COLUMN.
    if op.get_bind().dialect.name != "mssql":
        return

    # conversations
    op.alter_column('conversations', 'title',
                    type_=sa.Unicode(255), existing_nullable=True, existing_type=sa.String(255))

    # messages
    op.alter_column('messages', 'content',
                    type_=sa.UnicodeText(), existing_nullable=False, existing_type=sa.Text())
    op.alter_column('messages', 'tool_calls',
                    type_=sa.UnicodeText(), existing_nullable=True, existing_type=sa.Text())
    op.alter_column('messages', 'knowledge_refs',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))

    # agent_tasks
    op.alter_column('agent_tasks', 'title',
                    type_=sa.Unicode(500), existing_nullable=False, existing_type=sa.String(500))
    op.alter_column('agent_tasks', 'result_summary',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('agent_tasks', 'error_message',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))

    # workspaces
    op.alter_column('workspaces', 'display_name',
                    type_=sa.Unicode(255), existing_nullable=False, existing_type=sa.String(255))

    # projects
    op.alter_column('projects', 'display_name',
                    type_=sa.Unicode(255), existing_nullable=False, existing_type=sa.String(255))
    op.alter_column('projects', 'description',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('projects', 'fs_path',
                    type_=sa.Unicode(500), existing_nullable=True, existing_type=sa.String(500))

    # knowledge_metadata
    op.alter_column('knowledge_metadata', 'title',
                    type_=sa.Unicode(255), existing_nullable=False, existing_type=sa.String(255))
    op.alter_column('knowledge_metadata', 'fs_path',
                    type_=sa.Unicode(500), existing_nullable=False, existing_type=sa.String(500))
    op.alter_column('knowledge_metadata', 'tags',
                    type_=sa.Unicode(1000), existing_nullable=True, existing_type=sa.String(1000))
    op.alter_column('knowledge_metadata', 'summary',
                    type_=sa.Unicode(500), existing_nullable=False, existing_type=sa.String(500))

    # knowledge_versions
    op.alter_column('knowledge_versions', 'content',
                    type_=sa.UnicodeText(), existing_nullable=False, existing_type=sa.Text())
    op.alter_column('knowledge_versions', 'changed_by',
                    type_=sa.Unicode(100), existing_nullable=True, existing_type=sa.String(100))
    op.alter_column('knowledge_versions', 'change_summary',
                    type_=sa.Unicode(500), existing_nullable=True, existing_type=sa.String(500))

    # personal_memories
    op.alter_column('personal_memories', 'content',
                    type_=sa.Unicode(4000), existing_nullable=False, existing_type=sa.String(4000))
    op.alter_column('personal_memories', 'tags',
                    type_=sa.Unicode(500), existing_nullable=True, existing_type=sa.String(500))

    # episodic_memories
    op.alter_column('episodic_memories', 'summary',
                    type_=sa.Unicode(4000), existing_nullable=False, existing_type=sa.String(4000))
    op.alter_column('episodic_memories', 'key_findings',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('episodic_memories', 'open_questions',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))

    # agents
    op.alter_column('agents', 'display_name',
                    type_=sa.Unicode(255), existing_nullable=False, existing_type=sa.String(255))
    op.alter_column('agents', 'description',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('agents', 'definition_path',
                    type_=sa.Unicode(500), existing_nullable=True, existing_type=sa.String(500))
    op.alter_column('agents', 'system_prompt',
                    type_=sa.UnicodeText(), existing_nullable=True, existing_type=sa.String(8000))
    op.alter_column('agents', 'skills',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('agents', 'workflows',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))

    # automation_scripts
    op.alter_column('automation_scripts', 'name',
                    type_=sa.Unicode(255), existing_nullable=False, existing_type=sa.String(255))
    op.alter_column('automation_scripts', 'description',
                    type_=sa.Unicode(2000), existing_nullable=True, existing_type=sa.String(2000))
    op.alter_column('automation_scripts', 'content',
                    type_=sa.UnicodeText(), existing_nullable=False, existing_type=sa.Text())

    # script_runs
    op.alter_column('script_runs', 'stdout_log',
                    type_=sa.UnicodeText(), existing_nullable=True, existing_type=sa.Text())
    op.alter_column('script_runs', 'stderr_log',
                    type_=sa.UnicodeText(), existing_nullable=True, existing_type=sa.Text())

    # user_tokens
    op.alter_column('user_tokens', 'display_name',
                    type_=sa.Unicode(255), existing_nullable=True, existing_type=sa.String(255))


def downgrade() -> None:
    if op.get_bind().dialect.name != "mssql":
        return

    # user_tokens
    op.alter_column('user_tokens', 'display_name',
                    type_=sa.String(255), existing_nullable=True, existing_type=sa.Unicode(255))

    # script_runs
    op.alter_column('script_runs', 'stderr_log',
                    type_=sa.Text(), existing_nullable=True, existing_type=sa.UnicodeText())
    op.alter_column('script_runs', 'stdout_log',
                    type_=sa.Text(), existing_nullable=True, existing_type=sa.UnicodeText())

    # automation_scripts
    op.alter_column('automation_scripts', 'content',
                    type_=sa.Text(), existing_nullable=False, existing_type=sa.UnicodeText())
    op.alter_column('automation_scripts', 'description',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('automation_scripts', 'name',
                    type_=sa.String(255), existing_nullable=False, existing_type=sa.Unicode(255))

    # agents
    op.alter_column('agents', 'workflows',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('agents', 'skills',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('agents', 'system_prompt',
                    type_=sa.String(8000), existing_nullable=True, existing_type=sa.UnicodeText())
    op.alter_column('agents', 'definition_path',
                    type_=sa.String(500), existing_nullable=True, existing_type=sa.Unicode(500))
    op.alter_column('agents', 'description',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('agents', 'display_name',
                    type_=sa.String(255), existing_nullable=False, existing_type=sa.Unicode(255))

    # episodic_memories
    op.alter_column('episodic_memories', 'open_questions',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('episodic_memories', 'key_findings',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('episodic_memories', 'summary',
                    type_=sa.String(4000), existing_nullable=False, existing_type=sa.Unicode(4000))

    # personal_memories
    op.alter_column('personal_memories', 'tags',
                    type_=sa.String(500), existing_nullable=True, existing_type=sa.Unicode(500))
    op.alter_column('personal_memories', 'content',
                    type_=sa.String(4000), existing_nullable=False, existing_type=sa.Unicode(4000))

    # knowledge_versions
    op.alter_column('knowledge_versions', 'change_summary',
                    type_=sa.String(500), existing_nullable=True, existing_type=sa.Unicode(500))
    op.alter_column('knowledge_versions', 'changed_by',
                    type_=sa.String(100), existing_nullable=True, existing_type=sa.Unicode(100))
    op.alter_column('knowledge_versions', 'content',
                    type_=sa.Text(), existing_nullable=False, existing_type=sa.UnicodeText())

    # knowledge_metadata
    op.alter_column('knowledge_metadata', 'summary',
                    type_=sa.String(500), existing_nullable=False, existing_type=sa.Unicode(500))
    op.alter_column('knowledge_metadata', 'tags',
                    type_=sa.String(1000), existing_nullable=True, existing_type=sa.Unicode(1000))
    op.alter_column('knowledge_metadata', 'fs_path',
                    type_=sa.String(500), existing_nullable=False, existing_type=sa.Unicode(500))
    op.alter_column('knowledge_metadata', 'title',
                    type_=sa.String(255), existing_nullable=False, existing_type=sa.Unicode(255))

    # projects
    op.alter_column('projects', 'fs_path',
                    type_=sa.String(500), existing_nullable=True, existing_type=sa.Unicode(500))
    op.alter_column('projects', 'description',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('projects', 'display_name',
                    type_=sa.String(255), existing_nullable=False, existing_type=sa.Unicode(255))

    # workspaces
    op.alter_column('workspaces', 'display_name',
                    type_=sa.String(255), existing_nullable=False, existing_type=sa.Unicode(255))

    # agent_tasks
    op.alter_column('agent_tasks', 'error_message',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('agent_tasks', 'result_summary',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('agent_tasks', 'title',
                    type_=sa.String(500), existing_nullable=False, existing_type=sa.Unicode(500))

    # messages
    op.alter_column('messages', 'knowledge_refs',
                    type_=sa.String(2000), existing_nullable=True, existing_type=sa.Unicode(2000))
    op.alter_column('messages', 'tool_calls',
                    type_=sa.Text(), existing_nullable=True, existing_type=sa.UnicodeText())
    op.alter_column('messages', 'content',
                    type_=sa.Text(), existing_nullable=False, existing_type=sa.UnicodeText())

    # conversations
    op.alter_column('conversations', 'title',
                    type_=sa.String(255), existing_nullable=True, existing_type=sa.Unicode(255))
