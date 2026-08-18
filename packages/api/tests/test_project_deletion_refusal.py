"""Regression: delete_project commits the is_active flip BEFORE _check_delete.

The old order ran the multi-query check first; a turn claimed in the window
between the check passing and the flip commit saw is_active=True, persisted
messages, and the deletion transaction silently dropped them. A refused
deletion must also reopen the project (not freeze it) and record the refusal.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models.project import Project
from api.models.project_deletion_job import ProjectDeletionJob
from api.services import project_deletion as pd
from api.services.project_deletion import DeletionError, delete_project


@pytest.mark.asyncio
async def test_refused_deletion_restores_is_active_and_records_failed_job(db, make_project, monkeypatch):
    project = await make_project("refuse-del")
    pid = str(project.project_id)

    async def _refuse(db_, project_):
        raise DeletionError(409, "PROJECT_HAS_RUNNING_WORK", "Project has an active agent session running")

    monkeypatch.setattr(pd, "_check_delete", _refuse)

    with pytest.raises(DeletionError) as ei:
        await delete_project(db, pid, "test-user", project.slug)
    assert ei.value.code == "PROJECT_HAS_RUNNING_WORK"

    # The flip was committed before the check ran — the refusal must reopen
    # the project, or it stays frozen with no way to delete it later.
    fresh = (await db.execute(select(Project).where(Project.project_id == pid))).scalar_one()
    assert fresh.is_active is True

    # The refusal is recorded on the job row (prepared → failed), not leaked
    # as a phantom "prepared" job that startup_sweep would retry.
    jobs = (await db.execute(
        select(ProjectDeletionJob).where(ProjectDeletionJob.project_id == pid)
    )).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
