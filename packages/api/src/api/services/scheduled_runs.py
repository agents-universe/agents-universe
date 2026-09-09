"""Execution and result delivery for scheduled tasks.

Split out of ``scheduler.py`` so both the background loop and the router's
"run now" endpoint share one launch path. Each fire is one ``ScheduledTaskRun``
row plus, for script/playwright targets, a ``script_runs`` row created under the
task owner's identity — which is what makes the existing live-log WebSocket
(``/ws/script-runs/{run_id}``) and the project-deletion guard work unchanged.

Agent targets run headlessly through the same kernel the publish SSE endpoint
uses: ``run_turn(..., interactive=False)``. No client is attached, so a
DB-only transport is passed; the kernel persists the prompt and the reply into
the target conversation, which is the delivery.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from sqlalchemy import select, update

from agent_core.scripts.runner import execute_script, script_slot_guard
from api.database import AsyncSessionLocal
from api.models._compat import now_utc
from api.models.schedule import ScheduledTask, ScheduledTaskRun
from api.services.agent_turn import Transport

_log = logging.getLogger("agents_universe.scheduler")

# Headless agent turns are the only target that spends model tokens, so they get
# their own (small) cap; scripts and Playwright runs share the runner's global
# 3-slot guard with human-initiated runs.
AGENT_TURN_LIMIT = 2
_SUMMARY_CAP = 2000
_ERROR_CAP = 2000
_LOG_TAIL_CHARS = 1200

_agent_semaphore: asyncio.Semaphore | None = None
# Per-task launch locks: the "is a run already active?" check and the insert
# below are separate statements, so two concurrent launches (a tick racing a
# manual run-now) could both pass the check. The event loop is single-threaded,
# so a plain dict of locks is safe.
_spawn_locks: dict[str, asyncio.Lock] = {}


class ScheduleBusy(RuntimeError):
    """A manual launch was refused because the previous run is still active."""


class ScheduleNotFound(LookupError):
    """The scheduled task row no longer exists."""


def agent_turn_slot_guard() -> asyncio.Semaphore:
    """Lazy per-loop semaphore (an asyncio primitive binds to the running loop)."""
    global _agent_semaphore
    if _agent_semaphore is None:
        _agent_semaphore = asyncio.Semaphore(AGENT_TURN_LIMIT)
    return _agent_semaphore


class _NullTransport(Transport):
    """Records terminal turn events; there is no client to deliver to."""

    def __init__(self) -> None:
        self.stream_end: dict | None = None
        self.error: str | None = None

    async def send(self, conversation_id: str, data: dict) -> bool:
        if data.get("type") == "stream_end":
            self.stream_end = data
        elif data.get("type") == "error":
            self.error = data.get("message")
        return False


def _log_background_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        _log.error("Scheduled run task failed: %s", exc, exc_info=exc)


# ── Launch ───────────────────────────────────────────────────────────────────

async def spawn_run(app, schedule_id: str, *, trigger: str) -> str:
    """Create a ``ScheduledTaskRun`` and launch it in the background.

    Returns the run id. A scheduled fire whose previous run is still active is
    recorded as ``skipped`` (history stays honest, the task does not stack);
    a manual launch raises :class:`ScheduleBusy` instead, so the API can answer
    409 rather than pretending it ran.
    """
    lock = _spawn_locks.setdefault(schedule_id, asyncio.Lock())
    async with lock:
        async with AsyncSessionLocal() as db:
            task = (
                await db.execute(
                    select(ScheduledTask).where(ScheduledTask.schedule_id == schedule_id)
                )
            ).scalar_one_or_none()
            if task is None:
                raise ScheduleNotFound(schedule_id)

            active = (
                await db.execute(
                    select(ScheduledTaskRun.run_id)
                    .where(
                        ScheduledTaskRun.schedule_id == schedule_id,
                        ScheduledTaskRun.status.in_(("pending", "running")),
                    )
                    .limit(1)
                )
            ).first()
            if active:
                if trigger == "manual":
                    raise ScheduleBusy(schedule_id)
                run = ScheduledTaskRun(
                    schedule_id=schedule_id,
                    project_id=str(task.project_id),
                    trigger=trigger,
                    status="skipped",
                    conversation_id=task.conversation_id,
                    error="Previous run still in progress",
                    started_at=now_utc(),
                    completed_at=now_utc(),
                )
                db.add(run)
                await db.commit()
                return str(run.run_id)

            run = ScheduledTaskRun(
                schedule_id=schedule_id,
                project_id=str(task.project_id),
                trigger=trigger,
                status="pending",
                conversation_id=task.conversation_id,
                started_at=now_utc(),
            )
            db.add(run)
            await db.commit()
            run_id = str(run.run_id)

    bg = asyncio.create_task(execute_run(app, run_id))
    bg.add_done_callback(_log_background_failure)
    return run_id


# ── Execution ────────────────────────────────────────────────────────────────

class _Target:
    """Plain snapshot of the task row — never hold ORM instances across commits."""

    __slots__ = (
        "schedule_id", "name", "project_id", "target_type", "script_id",
        "spec_slug", "agent_slug", "prompt", "env_json", "conversation_id",
        "notify", "created_by",
    )

    def __init__(self, task: ScheduledTask) -> None:
        self.schedule_id = str(task.schedule_id)
        self.name = task.name
        self.project_id = str(task.project_id)
        self.target_type = task.target_type
        self.script_id = str(task.script_id) if task.script_id else None
        self.spec_slug = task.spec_slug
        self.agent_slug = task.agent_slug
        self.prompt = task.prompt
        self.env_json = task.env_json
        self.conversation_id = task.conversation_id
        self.notify = bool(task.notify)
        self.created_by = task.created_by


async def execute_run(app, run_id: str) -> None:
    """Body of one scheduled run. Never raises — failures land on the run row."""
    try:
        async with AsyncSessionLocal() as db:
            run = (
                await db.execute(
                    select(ScheduledTaskRun).where(ScheduledTaskRun.run_id == run_id)
                )
            ).scalar_one_or_none()
            if run is None:
                return
            if run.status == "skipped":  # written terminal at creation time
                return
            task_row = (
                await db.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.schedule_id == run.schedule_id
                    )
                )
            ).scalar_one_or_none()
            if task_row is None:
                run.status = "failed"
                run.error = "Scheduled task was deleted before the run started"
                run.completed_at = now_utc()
                await db.commit()
                return
            target = _Target(task_row)
            run.status = "running"
            run.started_at = now_utc()
            await db.commit()

        if target.target_type == "script":
            outcome = await _run_script_target(target)
        elif target.target_type == "playwright":
            outcome = await _run_playwright_target(target)
        elif target.target_type == "agent":
            outcome = await _run_agent_target(app, target)
        else:
            outcome = _Outcome("failed", None, f"Unknown target type: {target.target_type}")

        await _finalize(target, run_id, outcome)
    except asyncio.CancelledError:
        await _finalize_failure(run_id, "Execution cancelled by server shutdown")
        raise
    except Exception as exc:  # noqa: BLE001 - the run row is the error surface
        _log.exception("Scheduled run %s failed", run_id)
        await _finalize_failure(run_id, str(exc))


class _Outcome:
    __slots__ = ("status", "summary", "error", "script_run_id")

    def __init__(
        self,
        status: str,
        summary: str | None = None,
        error: str | None = None,
        script_run_id: str | None = None,
    ) -> None:
        self.status = status
        self.summary = summary
        self.error = error
        self.script_run_id = script_run_id


async def _finalize_failure(run_id: str, message: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            run = (
                await db.execute(
                    select(ScheduledTaskRun).where(ScheduledTaskRun.run_id == run_id)
                )
            ).scalar_one_or_none()
            if run is None or run.status in ("completed", "failed", "skipped"):
                return
            run.status = "failed"
            run.error = message[:_ERROR_CAP]
            run.completed_at = now_utc()
            await db.commit()
    except Exception:  # noqa: BLE001 - best effort, never mask the original error
        _log.exception("Could not mark scheduled run %s failed", run_id)


async def _finalize(target: _Target, run_id: str, outcome: _Outcome) -> None:
    """Write the terminal run state, mirror it onto the task, deliver the summary."""
    async with AsyncSessionLocal() as db:
        run = (
            await db.execute(
                select(ScheduledTaskRun).where(ScheduledTaskRun.run_id == run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            return
        run.status = outcome.status
        run.summary = (outcome.summary or "")[:_SUMMARY_CAP] or None
        run.error = (outcome.error or "")[:_ERROR_CAP] or None
        if outcome.script_run_id:
            run.script_run_id = outcome.script_run_id
        run.completed_at = now_utc()
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.schedule_id == target.schedule_id)
            .values(last_run_at=now_utc(), last_status=outcome.status)
        )
        await db.commit()

    if target.notify and outcome.status in ("completed", "failed"):
        await deliver_result(target, outcome)


# ── Targets ──────────────────────────────────────────────────────────────────

async def _run_script_target(target: _Target) -> _Outcome:
    from api.models.script import AutomationScript, ScriptRun
    from api.paths import resolve_project_fs_path

    sem = script_slot_guard()
    await sem.acquire()
    try:
        async with AsyncSessionLocal() as db:
            script = (
                await db.execute(
                    select(AutomationScript).where(
                        AutomationScript.script_id == target.script_id,
                        AutomationScript.project_id == target.project_id,
                    )
                )
            ).scalar_one_or_none()
            if script is None:
                return _Outcome("failed", None, "Script no longer exists in this project")
            if script.script_type == "playwright":
                return _Outcome("failed", None, "Playwright specs are scheduled as target_type=playwright")
            content, script_type = script.content, script.script_type

            run = ScriptRun(
                script_id=target.script_id,
                triggered_by=target.created_by,
                status="pending",
                started_at=now_utc(),
            )
            db.add(run)
            await db.commit()
            script_run_id = str(run.run_id)

            try:
                project_fs = await resolve_project_fs_path(target.project_id, db)
            except Exception as exc:  # noqa: BLE001
                return _Outcome("failed", None, f"Cannot resolve workspace: {exc}", script_run_id)

        await execute_script(
            script_run_id, content, script_type, target.created_by, project_fs
        )

        async with AsyncSessionLocal() as db:
            done = (
                await db.execute(
                    select(ScriptRun).where(ScriptRun.run_id == script_run_id)
                )
            ).scalar_one_or_none()
            if done is None:
                return _Outcome("failed", None, "Script run row disappeared", script_run_id)
            status = "completed" if done.status == "completed" else "failed"
            exit_code = done.exit_code
            log_text = (done.stdout_log or "") + (done.stderr_log or "")

        summary = _script_summary(target, status, exit_code, log_text)
        error = None if status == "completed" else f"Exit code {exit_code}"
        return _Outcome(status, summary, error, script_run_id)
    finally:
        sem.release()


async def _run_playwright_target(target: _Target) -> _Outcome:
    # Lazy import: the playwright executor lives with the scripts router, which
    # must not import the scheduler back (see routers/scripts.py).
    from api.models.script import ScriptRun
    from api.paths import resolve_project_fs_path
    from api.routers.scripts import (
        _execute_playwright,
        _get_or_create_playwright_anchor,
        _resolve_playwright_spec,
        _validated_playwright_env,
    )

    if not target.spec_slug:
        return _Outcome("failed", None, "No spec configured for this task")

    try:
        request_env = _validated_playwright_env(json.loads(target.env_json or "{}"))
    except (ValueError, TypeError):
        return _Outcome("failed", None, "env_json is not valid JSON")
    except Exception as exc:  # noqa: BLE001 - HTTPException from validation
        return _Outcome("failed", None, str(exc))

    sem = script_slot_guard()
    await sem.acquire()
    try:
        async with AsyncSessionLocal() as db:
            try:
                anchor = await _get_or_create_playwright_anchor(
                    db, target.project_id, target.created_by
                )
                anchor_id = str(anchor.script_id)
            except Exception as exc:  # noqa: BLE001
                return _Outcome("failed", None, f"Could not prepare the Playwright anchor: {exc}")

            run = ScriptRun(
                script_id=anchor_id,
                triggered_by=target.created_by,
                status="pending",
                started_at=now_utc(),
            )
            db.add(run)
            await db.commit()
            script_run_id = str(run.run_id)

            try:
                project_fs = await resolve_project_fs_path(target.project_id, db)
                _resolve_playwright_spec(project_fs, target.spec_slug)
            except Exception as exc:  # noqa: BLE001 - 404 HTTPException when missing
                return _Outcome("failed", None, str(exc), script_run_id)

        await _execute_playwright(
            script_run_id, target.spec_slug, request_env, target.created_by, project_fs
        )

        async with AsyncSessionLocal() as db:
            done = (
                await db.execute(
                    select(ScriptRun).where(ScriptRun.run_id == script_run_id)
                )
            ).scalar_one_or_none()
            if done is None:
                return _Outcome("failed", None, "Playwright run row disappeared", script_run_id)
            status = "completed" if done.status == "completed" else "failed"
            exit_code = done.exit_code
            log_text = (done.stdout_log or "") + (done.stderr_log or "")

        summary = _script_summary(target, status, exit_code, log_text)
        error = None if status == "completed" else f"Exit code {exit_code}"
        return _Outcome(status, summary, error, script_run_id)
    finally:
        sem.release()


async def _run_agent_target(app, target: _Target) -> _Outcome:
    from api.models.conversation import Conversation
    from api.services.agent_turn import run_turn
    from api.websocket.manager import manager

    sem = agent_turn_slot_guard()
    await sem.acquire()
    try:
        async with AsyncSessionLocal() as db:
            conversation_id = await _resolve_conversation(db, target)
            if conversation_id is None:
                return _Outcome("failed", None, "Target conversation is unavailable")

            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.conversation_id == conversation_id,
                        Conversation.project_id == target.project_id,
                        Conversation.user_id == target.created_by,
                        Conversation.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if conv is None:
                return _Outcome("failed", None, "Target conversation is unavailable")

        if not await manager.claim_turn(conversation_id):
            # A human is mid-turn in that conversation — do not interleave.
            return _Outcome("skipped", None, "Conversation is busy with another turn")
        try:
            manager.ensure_abort_event(conversation_id)
            manager.reset_abort(conversation_id)
            transport = _NullTransport()
            await run_turn(
                conversation_id,
                ws=SimpleNamespace(app=app),
                msg={
                    "content": f"[定时任务 · {target.name}]\n{target.prompt or ''}".strip(),
                    "agent_id": target.agent_slug,
                },
                user_id=target.created_by,
                transport=transport,
                interactive=False,
                actor_user_id=target.created_by,
            )
        finally:
            manager.release_turn(conversation_id)

        async with AsyncSessionLocal() as db:
            text = await _last_reply(db, conversation_id, transport)
        if not text:
            return _Outcome("failed", None, transport.error or "The agent produced no reply")
        return _Outcome("completed", text[:_SUMMARY_CAP])
    finally:
        sem.release()


async def _resolve_conversation(db, target: _Target) -> str | None:
    """Target conversation id, creating one for the task owner on first use."""
    if target.conversation_id:
        return target.conversation_id

    from api.models.agent import Agent
    from api.models.conversation import Conversation

    agent_id = None
    if target.agent_slug:
        agent = (
            await db.execute(select(Agent).where(Agent.slug == target.agent_slug))
        ).scalars().first()
        agent_id = str(agent.agent_id) if agent else None

    conv = Conversation(
        project_id=target.project_id,
        user_id=target.created_by,
        agent_id=agent_id,
        title=f"定时任务: {target.name}",
    )
    db.add(conv)
    await db.commit()
    conversation_id = str(conv.conversation_id)

    # Write the id back so later fires reuse the same thread.
    await db.execute(
        update(ScheduledTask)
        .where(ScheduledTask.schedule_id == target.schedule_id)
        .values(conversation_id=conversation_id)
    )
    await db.commit()
    return conversation_id


async def _last_reply(db, conversation_id: str, transport: _NullTransport) -> str:
    """Content of the assistant message this turn persisted."""
    from api.models.conversation import Message as DbMessage

    message_id = (transport.stream_end or {}).get("message_id")
    if message_id:
        content = (
            await db.execute(
                select(DbMessage.content).where(DbMessage.message_id == message_id)
            )
        ).scalar_one_or_none()
        if content:
            return content
    return (
        await db.execute(
            select(DbMessage.content)
            .where(
                DbMessage.conversation_id == conversation_id,
                DbMessage.role == "assistant",
            )
            .order_by(DbMessage.sequence_num.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or ""


def _script_summary(
    target: _Target, status: str, exit_code: int | None, log_text: str
) -> str:
    tail = log_text[-_LOG_TAIL_CHARS:].strip()
    lines = [
        f"定时任务「{target.name}」{_status_label(status)}",
        f"退出码: {exit_code}",
    ]
    if tail:
        lines.append("日志尾部:")
        lines.append(tail)
    return "\n".join(lines)


def _status_label(status: str) -> str:
    return {"completed": "执行成功", "failed": "执行失败", "skipped": "已跳过"}.get(
        status, status
    )


# ── Result delivery ──────────────────────────────────────────────────────────

async def deliver_result(target: _Target, outcome: _Outcome) -> None:
    """Append the run summary to the task's conversation as an assistant message.

    Deliberately silent on every failure path: a deleted or busy conversation
    must not fail the run itself. The turn claim serializes the insert against
    a live user turn so sequence numbers cannot interleave.
    """
    if not target.conversation_id:
        return
    from api.models.conversation import Conversation
    from api.services.agent_turn import _persist_assistant_message
    from api.websocket.manager import manager

    async with AsyncSessionLocal() as db:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.conversation_id == target.conversation_id,
                    Conversation.project_id == target.project_id,
                    Conversation.user_id == target.created_by,
                    Conversation.status == "active",
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            _log.info(
                "Skipping result delivery for schedule %s: conversation unavailable",
                target.schedule_id,
            )
            return

    claimed = False
    for _ in range(5):
        if await manager.claim_turn(target.conversation_id):
            claimed = True
            break
        await asyncio.sleep(2)
    if not claimed:
        _log.info(
            "Skipping result delivery for schedule %s: conversation busy",
            target.schedule_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            await _persist_assistant_message(
                db,
                target.conversation_id,
                outcome.summary or outcome.error or "(no output)",
                [],
                agent_slug=None,
                model_name=None,
            )
        await manager.send(
            target.conversation_id, {"type": "conversation_updated"}
        )
    except Exception:  # noqa: BLE001 - delivery is best-effort
        _log.exception(
            "Could not deliver scheduled result to conversation %s",
            target.conversation_id,
        )
    finally:
        manager.release_turn(target.conversation_id)
