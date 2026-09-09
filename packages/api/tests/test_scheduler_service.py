"""Scheduler loop: claim semantics, skip-on-miss, startup sweep, overlap guard."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from api.models._compat import now_utc
from api.models.schedule import ScheduledTask, ScheduledTaskRun
from api.services import scheduler as sched
from api.services import scheduled_runs


async def _task(db, project_id: str, **overrides) -> ScheduledTask:
    values = {
        "project_id": project_id,
        "name": "nightly",
        "target_type": "script",
        "cron_expr": "0 9 * * *",
        "timezone": "UTC",
        "enabled": True,
        "created_by": "test-user",
        "next_run_at": now_utc() - timedelta(minutes=1),
    }
    values.update(overrides)
    task = ScheduledTask(**values)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


class TestStartupSweep:
    async def test_settles_stale_runs_and_reanchors(self, db, make_project):
        project = await make_project()
        task = await _task(db, str(project.project_id))
        db.add(ScheduledTaskRun(
            schedule_id=str(task.schedule_id),
            project_id=str(project.project_id),
            trigger="schedule",
            status="running",
        ))
        await db.commit()

        settled, advanced = await sched.startup_sweep(db)
        assert settled == 1
        assert advanced == 1

        await db.refresh(task)
        assert task.next_run_at > now_utc()
        # Scoped to this task: the suite shares one DB file, so an unscoped
        # select could see runs written by other test modules.
        run = (
            await db.execute(
                select(ScheduledTaskRun).where(
                    ScheduledTaskRun.schedule_id == task.schedule_id
                )
            )
        ).scalars().one()
        assert run.status == "failed"
        assert "restart" in run.error

    async def test_skips_tasks_already_in_the_future(self, db, make_project):
        project = await make_project()
        future = now_utc() + timedelta(hours=1)
        task = await _task(db, str(project.project_id), next_run_at=future)
        settled, advanced = await sched.startup_sweep(db)
        assert (settled, advanced) == (0, 0)
        await db.refresh(task)
        assert task.next_run_at == future

    async def test_disables_task_with_unparsable_cron(self, db, make_project):
        project = await make_project()
        task = await _task(db, str(project.project_id), cron_expr="0 9 * *")
        await sched.startup_sweep(db)
        await db.refresh(task)
        assert task.enabled is False
        assert task.next_run_at is None


class TestTick:
    async def test_claims_due_task_once_and_advances(
        self, db, make_project, monkeypatch
    ):
        project = await make_project()
        task = await _task(db, str(project.project_id))
        launched: list[str] = []

        async def fake_spawn(app, schedule_id, *, trigger):
            launched.append(schedule_id)
            return "run-1"

        monkeypatch.setattr(sched, "spawn_run", fake_spawn)
        await sched._tick(None)
        await sched._tick(None)

        assert launched == [str(task.schedule_id)]
        await db.refresh(task)
        assert task.next_run_at > now_utc()
        assert task.last_run_at is not None

    async def test_missed_occurrences_are_skipped_not_replayed(
        self, db, make_project, monkeypatch
    ):
        project = await make_project()
        # Last fired three days ago — a daily task must not queue 3 catch-ups.
        task = await _task(
            db, str(project.project_id),
            next_run_at=now_utc() - timedelta(days=3),
        )
        launched: list[str] = []

        async def fake_spawn(app, schedule_id, *, trigger):
            launched.append(schedule_id)
            return "run-1"

        monkeypatch.setattr(sched, "spawn_run", fake_spawn)
        await sched._tick(None)

        assert launched == [str(task.schedule_id)]
        await db.refresh(task)
        assert task.next_run_at - now_utc() <= timedelta(days=1)

    async def test_ignores_tasks_of_inactive_projects(
        self, db, make_project, monkeypatch
    ):
        project = await make_project()
        project.is_active = False
        await db.commit()
        await _task(db, str(project.project_id))

        launched: list[str] = []

        async def fake_spawn(app, schedule_id, *, trigger):
            launched.append(schedule_id)
            return "run-1"

        monkeypatch.setattr(sched, "spawn_run", fake_spawn)
        await sched._tick(None)
        assert launched == []

    async def test_ignores_disabled_tasks(self, db, make_project, monkeypatch):
        project = await make_project()
        await _task(db, str(project.project_id), enabled=False)
        monkeypatch.setattr(sched, "spawn_run", lambda *a, **k: None)
        await sched._tick(None)  # must not raise


class TestSpawnRun:
    async def test_records_skipped_when_previous_run_is_active(self, db, make_project):
        project = await make_project()
        task = await _task(db, str(project.project_id))
        db.add(ScheduledTaskRun(
            schedule_id=str(task.schedule_id),
            project_id=str(project.project_id),
            trigger="schedule",
            status="running",
        ))
        await db.commit()

        run_id = await scheduled_runs.spawn_run(None, str(task.schedule_id), trigger="schedule")
        run = (
            await db.execute(
                select(ScheduledTaskRun).where(ScheduledTaskRun.run_id == run_id)
            )
        ).scalars().one()
        assert run.status == "skipped"
        assert "still in progress" in run.error

    async def test_manual_launch_raises_when_busy(self, db, make_project):
        project = await make_project()
        task = await _task(db, str(project.project_id))
        db.add(ScheduledTaskRun(
            schedule_id=str(task.schedule_id),
            project_id=str(project.project_id),
            trigger="schedule",
            status="pending",
        ))
        await db.commit()

        with pytest.raises(scheduled_runs.ScheduleBusy):
            await scheduled_runs.spawn_run(None, str(task.schedule_id), trigger="manual")

    async def test_unknown_schedule_raises(self, db):
        with pytest.raises(scheduled_runs.ScheduleNotFound):
            await scheduled_runs.spawn_run(None, "nope", trigger="manual")


class TestStartStop:
    async def test_disabled_by_settings(self, monkeypatch):
        from api.config import get_settings

        monkeypatch.setattr(
            get_settings(), "scheduler_enabled", False, raising=False
        )
        assert sched.start_scheduler(None) is None

    async def test_start_then_stop(self, monkeypatch):
        from api.config import get_settings

        monkeypatch.setattr(
            get_settings(), "scheduler_enabled", True, raising=False
        )
        ticks: list[int] = []

        async def fake_tick(app):
            ticks.append(1)

        monkeypatch.setattr(sched, "_tick", fake_tick)
        monkeypatch.setattr(sched, "POLL_SECONDS", 0.01)
        task = sched.start_scheduler(None)
        assert task is not None
        await asyncio.sleep(0.05)
        await sched.stop_scheduler(task)
        assert task.done()
        assert ticks
