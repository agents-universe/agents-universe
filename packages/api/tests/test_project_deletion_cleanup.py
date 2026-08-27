"""Regression: project deletion must clean up conversation_runs rows.

The leaf-table delete block previously skipped conversation_runs. On MSSQL /
PostgreSQL the FK carries ON DELETE CASCADE, but SQLite (local dev + test DB)
never enforces FKs — deleting the Conversation left orphaned run rows behind.
Deletion must be explicit and identical across every dialect.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models.conversation import Conversation
from api.models.conversation_run import ConversationRun
from api.models.project import Project
from api.services.project_deletion import delete_project


@pytest.mark.asyncio
async def test_delete_project_removes_conversation_runs(db, make_project):
    project = await make_project("run-cleanup")
    pid = str(project.project_id)

    conv = Conversation(project_id=pid, user_id="test-user", status="completed")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    run = ConversationRun(conversation_id=conv.conversation_id, status="completed")
    db.add(run)
    await db.commit()

    await delete_project(db, pid, "test-user", project.slug)

    leftover = (
        await db.execute(
            select(ConversationRun).where(ConversationRun.conversation_id == conv.conversation_id)
        )
    ).scalar_one_or_none()
    assert leftover is None, "conversation_runs row must be deleted with its conversation"

    proj = (
        await db.execute(select(Project).where(Project.project_id == pid))
    ).scalar_one_or_none()
    assert proj is None
