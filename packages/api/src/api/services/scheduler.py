"""In-process cron scheduler for ``scheduled_tasks``.

Single-replica by design: ``docker-entrypoint.sh`` starts one uvicorn process
without ``--workers``, and the loop lives in that process's event loop. The
due-task claim is still a compare-and-swap on ``next_run_at`` so a second
replica (or a duplicate loop) can never fire the same occurrence twice — but
replicas > 1 would additionally need shared turn claims (``manager.claim_turn``
is per-process) and a lease for in-flight runs. Keep one replica.

Missed occurrences are skipped, never caught up: the next fire time is computed
as the first match strictly after *now*, so downtime of any length collapses to
a single future point.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update

from agent_core.scheduling.cron import CronError, next_run_at
from api.database import AsyncSessionLocal
from api.models._compat import now_utc
from api.models.project import Project
from api.models.schedule import ScheduledTask, ScheduledTaskRun
from api.services.scheduled_runs import ScheduleBusy, ScheduleNotFound, spawn_run

_log = logging.getLogger("agents_universe.scheduler")

POLL_SECONDS = 20
_DUE_BATCH = 20
_STOP_TIMEOUT = 10


def start_scheduler(app) -> asyncio.Task | None:
    """Start the loop; returns the task (``None`` when disabled)."""
    from api.config import get_settings

    if not get_settings().scheduler_enabled:
        _log.info("Scheduler disabled by configuration")
        return None
    task = asyncio.create_task(_loop(app))
    _log.info("Scheduler started (poll every %ds)", POLL_SECONDS)
    return task


async def stop_scheduler(task: asyncio.Task | None) -> None:
    """Cancel the loop and wait for it to unwind.

    In-flight runs are NOT cancelled — a script or an agent turn mid-flight
    should finish or be settled by the next startup sweep, matching how
    background turns survive a shutdown elsewhere in the app.
    """
    if task is None:
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=_STOP_TIMEOUT)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:  # noqa: BLE001
        _log.exception("Scheduler loop ended with an error")


async def startup_sweep(db) -> tuple[int, int]:
    """Settle runs from a dead process and re-anchor every stale fire time.

    Returns ``(settled_runs, advanced_tasks)``. Single-replica assumption, same
    as ``interrupt_stale_runs``.
    """
    now = now_utc()
    settled = await db.execute(
        update(ScheduledTaskRun)
        .where(ScheduledTaskRun.status.in_(("pending", "running")))
        .values(
            status="failed",
            error="Interrupted by server restart",
            completed_at=now,
        )
    )

    advanced = 0
    tasks = (
        await db.execute(
            select(ScheduledTask).where(
                ScheduledTask.enabled == True,  # noqa: E712
                ScheduledTask.next_run_at.is_not(None),
                ScheduledTask.next_run_at < now,
            )
        )
    ).scalars().all()
    for task in tasks:
        try:
            upcoming = next_run_at(task.cron_expr, task.timezone, now)
        except CronError as exc:
            _log.warning(
                "Disabling schedule %s: invalid cron %r (%s)",
                task.schedule_id, task.cron_expr, exc,
            )
            task.enabled = False
            task.next_run_at = None
            advanced += 1
            continue
        task.next_run_at = upcoming
        advanced += 1
    await db.commit()
    return settled.rowcount or 0, advanced


async def _loop(app) -> None:
    while True:
        try:
            await _tick(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
            _log.exception("Scheduler tick failed")
        await asyncio.sleep(POLL_SECONDS)


async def _tick(app) -> None:
    now = now_utc()
    async with AsyncSessionLocal() as db:
        due = (
            await db.execute(
                select(ScheduledTask)
                .join(Project, Project.project_id == ScheduledTask.project_id)
                .where(
                    ScheduledTask.enabled == True,  # noqa: E712
                    ScheduledTask.next_run_at.is_not(None),
                    ScheduledTask.next_run_at <= now,
                    Project.is_active == True,  # noqa: E712
                )
                .order_by(ScheduledTask.next_run_at)
                .limit(_DUE_BATCH)
            )
        ).scalars().all()

        claimed: list[str] = []
        for task in due:
            schedule_id = str(task.schedule_id)
            observed = task.next_run_at
            try:
                upcoming = next_run_at(task.cron_expr, task.timezone, now)
            except CronError as exc:
                _log.warning(
                    "Disabling schedule %s: invalid cron %r (%s)",
                    schedule_id, task.cron_expr, exc,
                )
                await db.execute(
                    update(ScheduledTask)
                    .where(ScheduledTask.schedule_id == schedule_id)
                    .values(enabled=False, next_run_at=None)
                )
                continue

            # Compare-and-swap: the WHERE clause is the claim. A concurrent
            # writer (another replica, or this loop racing itself) that already
            # advanced next_run_at makes rowcount 0 and we skip.
            result = await db.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.schedule_id == schedule_id,
                    ScheduledTask.next_run_at == observed,
                )
                .values(next_run_at=upcoming, last_run_at=now)
            )
            if result.rowcount != 1:
                continue
            claimed.append(schedule_id)
        await db.commit()

    for schedule_id in claimed:
        try:
            await spawn_run(app, schedule_id, trigger="schedule")
        except ScheduleNotFound:
            _log.warning("Schedule %s vanished between claim and launch", schedule_id)
        except ScheduleBusy:  # pragma: no cover - scheduled path writes skipped
            pass
        except Exception:  # noqa: BLE001
            _log.exception("Could not launch scheduled run for %s", schedule_id)
