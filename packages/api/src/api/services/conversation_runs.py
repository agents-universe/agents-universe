"""Durable conversation-run lifecycle writes (best-effort, never raise).

A `conversation_runs` row records every agent turn so a process restart
(container restart, dev-server kill, laptop sleep) doesn't erase the trace of
a run that died in flight: the startup sweep flips leftover `running` rows to
`interrupted`, and the frontend surfaces the latest run (status, partial
text, rerun affordance) when the conversation is reopened.

Each write uses its own short-lived session — callers run inside
`forward_events` / `_handle_message` which own long-lived sessions, and
interleaved executes on a shared async session corrupt both.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import AsyncSessionLocal
from api.models._compat import now_utc
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
