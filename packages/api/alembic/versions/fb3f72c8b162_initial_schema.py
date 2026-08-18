"""initial_schema

Revision ID: fb3f72c8b162
Revises:
Create Date: 2026-06-20 08:06:41.147864

Schema notes:
- No `users` table: user identity comes from OAuth SSO (user_id = SSO sub claim).
  user_id columns are plain String(100) — no FK constraints to a users table.
- No embedding columns: knowledge loading uses full-text load, not vector search.
- Sessions are stored in Redis, not in DB.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'fb3f72c8b162'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('agent_id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=2000), nullable=True),
    sa.Column('definition_path', sa.String(length=500), nullable=True),
    sa.Column('model_low', sa.String(length=500), nullable=True),
    sa.Column('model_mid', sa.String(length=500), nullable=True),
    sa.Column('model_high', sa.String(length=500), nullable=True),
    # VARCHAR(8000) on utf8mb4 is 32KB; together with the other wide agents
    # columns the row exceeds MySQL's 65535-byte InnoDB limit (g8b3d0f2c451's
    # Unicode conversion is guarded away on MySQL, so it never shrinks here).
    # TEXT is stored off-page — SQL Server/PG/SQLite keep VARCHAR(8000).
    sa.Column('system_prompt', sa.String(length=8000).with_variant(sa.Text(), 'mysql'), nullable=True),
    sa.Column('skills', sa.String(length=2000), nullable=True),
    sa.Column('tools', sa.String(length=1000), nullable=True),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('agent_id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('user_tokens',
    sa.Column('token_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('service_key', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('encrypted_value', sa.String(length=4000), nullable=False),
    sa.Column('key_hint', sa.String(length=10), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('token_id')
    )
    op.create_table('workspaces',
    sa.Column('workspace_id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('owner_id', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('workspace_id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('projects',
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('workspace_id', sa.String(length=36), nullable=False),
    sa.Column('parent_id', sa.String(length=36), nullable=True),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=2000), nullable=True),
    sa.Column('fs_path', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['projects.project_id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.workspace_id'], ),
    sa.PrimaryKeyConstraint('project_id')
    )
    op.create_table('workspace_members',
    sa.Column('workspace_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('role', sa.String(length=50), nullable=False, server_default='member'),
    sa.Column('joined_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.workspace_id'], ),
    sa.PrimaryKeyConstraint('workspace_id', 'user_id')
    )
    op.create_table('automation_scripts',
    sa.Column('script_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=2000), nullable=True),
    sa.Column('script_type', sa.String(length=50), nullable=False, server_default='python'),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('script_id')
    )
    op.create_table('conversations',
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('agent_id', sa.String(length=36), nullable=True),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False, server_default='active'),
    sa.Column('token_budget', sa.Integer(), nullable=False, server_default='128000'),
    sa.Column('tokens_used', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.agent_id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('conversation_id')
    )
    op.create_table('knowledge_metadata',
    sa.Column('knowledge_id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('fs_path', sa.String(length=500), nullable=False),
    sa.Column('completeness_score', sa.Float(), nullable=False),
    sa.Column('coverage_breadth', sa.Float(), nullable=False),
    sa.Column('recency_score', sa.Float(), nullable=False),
    sa.Column('cross_ref_density', sa.Float(), nullable=False),
    sa.Column('agent_gap_score', sa.Float(), nullable=False),
    sa.Column('tags', sa.String(length=1000), nullable=True),
    sa.Column('cross_references', sa.String(length=2000), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('word_count', sa.Integer(), nullable=False),
    sa.Column('last_accessed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('is_archived', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('knowledge_id')
    )
    op.create_table('personal_memories',
    sa.Column('memory_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('content', sa.String(length=4000), nullable=False),
    sa.Column('tags', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('is_archived', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('memory_id')
    )
    op.create_table('agent_tasks',
    sa.Column('task_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('parent_task_id', sa.String(length=36), nullable=True),
    sa.Column('sequence_num', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False, server_default='pending'),
    sa.Column('tools_needed', sa.String(length=500), nullable=True),
    sa.Column('depends_on', sa.String(length=500), nullable=True),
    sa.Column('estimated_complexity', sa.String(length=20), nullable=True),
    sa.Column('actual_model', sa.String(length=100), nullable=True),
    sa.Column('result_summary', sa.String(length=2000), nullable=True),
    sa.Column('error_message', sa.String(length=2000), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.conversation_id'], ),
    sa.ForeignKeyConstraint(['parent_task_id'], ['agent_tasks.task_id'], ),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('episodic_memories',
    sa.Column('episode_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=100), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('summary', sa.String(length=4000), nullable=False),
    sa.Column('key_findings', sa.String(length=2000), nullable=True),
    sa.Column('open_questions', sa.String(length=2000), nullable=True),
    sa.Column('generated_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.conversation_id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('episode_id')
    )
    op.create_table('knowledge_versions',
    sa.Column('version_id', sa.String(length=36), nullable=False),
    sa.Column('knowledge_id', sa.String(length=36), nullable=False),
    sa.Column('version_num', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('changed_by', sa.String(length=100), nullable=True),
    sa.Column('change_summary', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['knowledge_id'], ['knowledge_metadata.knowledge_id'], ),
    sa.PrimaryKeyConstraint('version_id')
    )
    op.create_table('messages',
    sa.Column('message_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tool_calls', sa.Text(), nullable=True),
    sa.Column('knowledge_refs', sa.String(length=2000), nullable=True),
    sa.Column('token_count', sa.Integer(), nullable=True),
    sa.Column('sequence_num', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.conversation_id'], ),
    sa.PrimaryKeyConstraint('message_id')
    )
    op.create_table('script_runs',
    sa.Column('run_id', sa.String(length=36), nullable=False),
    sa.Column('script_id', sa.String(length=36), nullable=False),
    sa.Column('triggered_by', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=50), nullable=False, server_default='pending'),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('stdout_log', sa.Text(), nullable=True),
    sa.Column('stderr_log', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['script_id'], ['automation_scripts.script_id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )


def downgrade() -> None:
    op.drop_table('script_runs')
    op.drop_table('messages')
    op.drop_table('knowledge_versions')
    op.drop_table('episodic_memories')
    op.drop_table('agent_tasks')
    op.drop_table('personal_memories')
    op.drop_table('knowledge_metadata')
    op.drop_table('conversations')
    op.drop_table('automation_scripts')
    op.drop_table('workspace_members')
    op.drop_table('projects')
    op.drop_table('workspaces')
    op.drop_table('user_tokens')
    op.drop_table('agents')
