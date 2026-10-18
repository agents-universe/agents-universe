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
from api.models.publish import AgentPublish, PublishKey
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


@pytest.mark.asyncio
async def test_delete_project_removes_published_agents_and_keys(db, make_project):
    project = await make_project("publish-cleanup")
    pid = str(project.project_id)
    publish = AgentPublish(
        owner_id="test-user",
        agent_slug="support-agent",
        project_id=pid,
        model_config_id="model-config",
    )
    db.add(publish)
    await db.flush()
    publish_id = str(publish.publish_id)
    key = PublishKey(
        publish_id=publish_id,
        key_hash="a" * 64,
        key_hint="1234",
    )
    db.add(key)
    await db.commit()

    await delete_project(db, pid, "test-user", project.slug)

    assert (await db.execute(
        select(AgentPublish).where(AgentPublish.project_id == pid)
    )).scalars().all() == []
    assert (await db.execute(
        select(PublishKey).where(PublishKey.publish_id == publish_id)
    )).scalars().all() == []


def test_delete_project_sweeps_every_table_in_projects_fk_closure():
    """Tripwire for the hand-maintained DELETE sweep in delete_project.

    The sweep is derived by hand from ~25 tables. agent_publishes was once
    forgotten: FK violations looped deletion into a 503 retry on
    Postgres/MySQL/MSSQL and orphaned rows on SQLite. Whenever a new table
    joins the FK closure of `projects`, delete_project must name its model
    or the same failure comes back.
    """
    import inspect

    from api.database import Base
    from api.services import project_deletion

    source = inspect.getsource(project_deletion.delete_project)

    table_to_model = {
        mapper.local_table.name: mapper.class_.__name__
        for mapper in Base.registry.mappers
    }

    # FK closure: a table joins when any of its FK targets already joined.
    reachable = {"projects"}
    changed = True
    while changed:
        changed = False
        for table in Base.metadata.tables.values():
            if table.name in reachable:
                continue
            parents = {
                fk.target_fullname.split(".")[0] for fk in table.foreign_keys
            }
            if parents & reachable:
                reachable.add(table.name)
                changed = True

    missing = []
    for name in sorted(reachable - {"projects"}):
        model_name = table_to_model.get(name)
        if model_name is None:
            continue  # auxiliary table without a mapped class
        if f"{model_name}" not in source:
            missing.append(f"{model_name} ({name})")
    assert not missing, (
        "delete_project's sweep does not mention: "
        + ", ".join(missing)
        + " — delete them (leaf-first) before deleting their parents"
    )


@pytest.mark.asyncio
async def test_startup_sweep_survives_one_failed_job(db, make_project, monkeypatch):
    """A rollback in one job's handler expires every ORM instance. Reading
    job.job_id afterwards raised MissingGreenlet and aborted the sweep, so
    the remaining durable jobs were never retried."""
    from sqlalchemy import update

    from api.models.project_deletion_job import ProjectDeletionJob
    from api.services.project_deletion import startup_sweep

    project = await make_project("sweep-survivor")
    pid = str(project.project_id)
    await db.execute(
        update(Project).where(Project.project_id == pid).values(is_active=False)
    )
    # The unsafe slug makes _safe_paths raise before touching state — the
    # handler must log and continue, not blow up on the expired ORM row.
    bad = ProjectDeletionJob(
        project_id="p-bad", slug="../escape",
        source_path="/tmp/x", trash_path="/tmp/y", status="prepared",
    )
    good = ProjectDeletionJob(
        project_id=pid, slug=project.slug,
        source_path="/tmp/source", trash_path="/tmp/trash",
        status="prepared",
    )
    db.add_all([bad, good])
    await db.commit()
    bad_id, good_id = str(bad.job_id), str(good.job_id)

    await startup_sweep(db)

    bad_row = (await db.execute(
        select(ProjectDeletionJob).where(ProjectDeletionJob.job_id == bad_id)
    )).scalar_one()
    good_row = (await db.execute(
        select(ProjectDeletionJob).where(ProjectDeletionJob.job_id == good_id)
    )).scalar_one()
    # The bad job stays prepared (its handler rolled back), but the sweep
    # continued: the surviving project was reopened and its job settled.
    assert bad_row.status == "prepared"
    assert good_row.status == "failed"
    reopened = (await db.execute(
        select(Project).where(Project.project_id == pid)
    )).scalar_one()
    assert reopened.is_active is True
