"""Durable conversation-run lifecycle writes (best-effort, never raise).

A `conversation_runs` row records every agent turn so a process restart
(container restart, dev-server kill, laptop sleep) doesn't erase the trace of
a run that died in flight: the startup sweep flips leftover `running` rows to
`interrupted`, materializes their partial output into the message history
(so the next turn's agent context includes it), and settles stale task rows.
The frontend surfaces the latest run status when the conversation is
reopened - the user continues the interrupted task by typing, not by
re-running.

Each write uses its own short-lived session — callers run inside
`forward_events` / `_handle_message` which own long-lived sessions, and
interleaved executes on a shared async session corrupt both.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import AsyncSessionLocal
from api.models._compat import now_utc
from api.models.conversation import AgentTask, Conversation
from api.models.conversation import Message as DbMessage
from api.models.conversation_run import ConversationRun

_log = logging.getLogger("agents_universe.runs")

# Snapshot cap: partial-text recovery only — the Message row is authoritative
# for completed turns.
_SNAPSHOT_MAX_CHARS = 50_000


def _cap(text: str | None) -> str:
    if not text:
        return ""
    return text[-_SNAPSHOT_MAX_CHARS:]


async def create_run(conversation_id: str, user_message_id: str | None) -> str:
    """Insert a `running` row and return its run_id. Best-effort."""
    async with AsyncSessionLocal() as db:
        run = ConversationRun(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
        )
        db.add(run)
        await db.commit()
        return run.run_id


async def update_run_snapshot(run_id: str, text: str) -> None:
    """Throttled partial-text snapshot (recovery only)."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(ConversationRun)
            .where(
                ConversationRun.run_id == run_id,
                ConversationRun.status == "running",
            )
            .values(streaming_snapshot=_cap(text))
        )
        await db.commit()


async def finish_run(
    run_id: str,
    status: str,
    *,
    error_message: str | None = None,
    tokens_used: int | None = None,
    snapshot: str | None = None,
) -> None:
    """Terminal transition running → completed | failed | interrupted.

    Guarded by ``status == 'running'`` so a racing terminal write (e.g. the
    finally-tail safety net after a normal finish) is a no-op.
    """
    async with AsyncSessionLocal() as db:
        values: dict = {"status": status, "ended_at": now_utc()}
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        if tokens_used is not None:
            values["tokens_used"] = tokens_used
        if snapshot is not None:
            values["streaming_snapshot"] = _cap(snapshot)
        await db.execute(
            update(ConversationRun)
            .where(
                ConversationRun.run_id == run_id,
                ConversationRun.status == "running",
            )
            .values(**values)
        )
        await db.commit()


async def interrupt_stale_runs(db: AsyncSession) -> int:
    """Startup sweep: every row still `running` belonged to a dead process.

    Single-replica assumption: in a multi-replica deployment replica B's
    sweep could interrupt replica A's genuinely running turn — same class of
    known limitation as the concurrent auto-migrate retry (main.py).
    """
    result = await db.execute(
        update(ConversationRun)
        .where(ConversationRun.status == "running")
        .values(status="interrupted", ended_at=now_utc())
    )
    await db.commit()
    return result.rowcount or 0


async def latest_run(db: AsyncSession, conversation_id: str) -> ConversationRun | None:
    result = await db.execute(
        select(ConversationRun)
        .where(ConversationRun.conversation_id == conversation_id)
        .order_by(ConversationRun.started_at.desc(), ConversationRun.run_id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def materialize_interrupted_snapshots(db: AsyncSession) -> int:
    """Recover interrupted partial output into the message history.

    A hard-killed run (process death before stream_end) never persisted its
    assistant message - the partial text survives only in the throttled
    `streaming_snapshot`. Materialize it as an `interrupted` assistant row so
    BOTH the reopened conversation and the next turn's agent context see it:
    the user continues the interrupted task by typing instead of re-running.

    Invariant maintained: a non-null snapshot on an interrupted run means the
    partial output is NOT yet in messages. Every processed run (materialized
    or deduped) has its snapshot cleared, which also makes the sweep
    idempotent. Runs flipped by the stream_end path already carry their
    partial in a message row - the dedup guard below recognizes them.

    Only the LATEST interrupted run per conversation is recovered; an older
    snapshot buried under newer turns is stale and just cleared. Startup-only
    (single-replica assumption, same as interrupt_stale_runs).
    """
    import json as _json

    result = await db.execute(
        select(ConversationRun)
        .where(
            ConversationRun.status == "interrupted",
            ConversationRun.streaming_snapshot.is_not(None),
            ConversationRun.streaming_snapshot != "",
        )
        .order_by(ConversationRun.started_at.asc(), ConversationRun.run_id.asc())
    )
    stale_runs = result.scalars().all()
    if not stale_runs:
        return 0

    # Copy the needed attributes up front: the per-run commit below expires
    # the ORM instances, and touching an expired attribute in async context
    # raises MissingGreenlet.
    latest_per_conv: dict[str, tuple[str, str | None, str]] = {}
    for run in stale_runs:  # ascending order: latest run per conversation wins
        latest_per_conv[run.conversation_id] = (
            run.run_id,
            run.user_message_id,
            run.streaming_snapshot or "",
        )

    materialized = 0
    latest_run_ids: set[str] = set()
    for conversation_id, (run_id, user_message_id, snapshot) in latest_per_conv.items():
        latest_run_ids.add(run_id)
        try:
            user_seq: int | None = None
            if user_message_id:
                user_seq = (
                    await db.execute(
                        select(DbMessage.sequence_num).where(
                            DbMessage.message_id == user_message_id
                        )
                    )
                ).scalar_one_or_none()
            if user_seq is not None:
                # Dedup: an assistant row after the run's user message means
                # the interrupted turn already persisted its output (stream_end
                # path, or a later turn's reply) - the snapshot is a duplicate.
                has_reply = (
                    await db.execute(
                        select(func.count())
                        .select_from(DbMessage)
                        .where(
                            DbMessage.conversation_id == conversation_id,
                            DbMessage.role == "assistant",
                            DbMessage.sequence_num > user_seq,
                        )
                    )
                ).scalar_one()
                if not has_reply:
                    # Serialize sequence assignment like _persist_assistant_message
                    # (lock is a no-op on SQLite, real on the other dialects).
                    await db.execute(
                        select(Conversation.conversation_id)
                        .where(Conversation.conversation_id == conversation_id)
                        .with_for_update()
                    )
                    max_seq = (
                        await db.execute(
                            select(func.coalesce(func.max(DbMessage.sequence_num), 0))
                            .where(DbMessage.conversation_id == conversation_id)
                        )
                    ).scalar_one()
                    db.add(DbMessage(
                        message_id=str(uuid.uuid4()),
                        conversation_id=conversation_id,
                        role="assistant",
                        content=snapshot,
                        knowledge_refs=_json.dumps({"interrupted": True}),
                        sequence_num=max_seq + 1,
                    ))
                    materialized += 1
            await db.execute(
                update(ConversationRun)
                .where(ConversationRun.run_id == run_id)
                .values(streaming_snapshot=None)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            _log.warning(
                "Snapshot materialization failed for run %s",
                run_id,
                exc_info=True,
            )

    # Non-latest runs were skipped on purpose - clear their snapshots too, or
    # every later sweep re-processes a partial that is stale by position.
    older_run_ids = [
        run.run_id for run in stale_runs if run.run_id not in latest_run_ids
    ]
    if older_run_ids:
        try:
            await db.execute(
                update(ConversationRun)
                .where(ConversationRun.run_id.in_(older_run_ids))
                .values(streaming_snapshot=None)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            _log.warning("Snapshot clearing failed for older runs", exc_info=True)
    return materialized


async def interrupt_stale_tasks(db: AsyncSession) -> int:
    """Startup sweep: settle agent_tasks still `running` from a dead process.

    Task execution lives in the (now dead) process's memory, so a `running`
    row is fiction: the plan context would tell the next agent a task is
    actively executing, and the conversation list counts it as active work.
    Flip to `failed` (the honest terminal state - it never completed) with a
    short reason; `pending` rows stay pending so the plan remains resumable.

    Single-replica assumption, same as interrupt_stale_runs.
    """
    result = await db.execute(
        update(AgentTask)
        .where(AgentTask.status == "running")
        .values(
            status="failed",
            error_message="Turn interrupted by process restart",
            completed_at=now_utc(),
        )
    )
    await db.commit()
    return result.rowcount or 0
