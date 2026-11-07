"""Project usage dashboard router (the 用量 tab).

Single endpoint returning summary + top-10 users + daily trend over one
shared time window, so the three sections can never disagree about the
range they describe.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_project, get_current_user
from api.models._compat import now_utc
from api.models.conversation import Conversation
from api.models.conversation_run import ConversationRun
from api.models.project import Project

router = APIRouter(prefix="/api")

_TOP_N = 10


async def _usage_payload(db: AsyncSession, project_id: str, days: int) -> dict:
    now = now_utc()
    today = now.date()
    # Calendar-aligned UTC window: `days` whole days ending today, so the
    # daily buckets number exactly `days` with no off-by-one against the
    # row filter.
    since_date = today - timedelta(days=days - 1)
    since = datetime.combine(since_date, time.min, tzinfo=timezone.utc)

    # conversation_runs.tokens_used is a CUMULATIVE per-conversation ledger
    # (agent_turn seeds the session with conv.tokens_used; finish_run persists
    # session.tokens_used) — a plain SUM would count every prior turn again.
    # Per-turn usage is the running-max delta: MAX of the prior rows in the
    # conversation subtracted from the current value. Running-max rather than
    # LAG so a failed turn's 0 write (finish_run(..., tokens_used=0)) and
    # legacy NULLs produce neither negative deltas nor a broken chain.
    # This query silently breaks if run.tokens_used ever changes semantics
    # to per-turn deltas — keep the two in sync.
    #
    # The time filter MUST live in the outer query: SQL evaluates WHERE
    # before window functions, so filtering inside would leave the first
    # in-window run with no prior rows and dump the conversation's entire
    # lifetime total into the window.
    prev_max = func.max(ConversationRun.tokens_used).over(
        partition_by=ConversationRun.conversation_id,
        order_by=[ConversationRun.started_at, ConversationRun.run_id],
        rows=(None, -1),
    )
    diff = ConversationRun.tokens_used - func.coalesce(prev_max, 0)
    delta = case(
        (ConversationRun.tokens_used.is_(None), 0),
        (diff < 0, 0),
        else_=diff,
    )

    inner = (
        select(
            Conversation.user_id.label("user_id"),
            Conversation.conversation_id.label("conversation_id"),
            ConversationRun.started_at.label("started_at"),
            delta.label("delta"),
        )
        .select_from(ConversationRun)
        .join(
            Conversation,
            Conversation.conversation_id == ConversationRun.conversation_id,
        )
        .where(Conversation.project_id == project_id)
        .subquery()
    )
    stmt = (
        select(
            inner.c.user_id,
            inner.c.conversation_id,
            inner.c.started_at,
            inner.c.delta,
        )
        .where(inner.c.started_at >= since)
        .order_by(inner.c.user_id, inner.c.started_at)
    )
    rows = (await db.execute(stmt)).all()

    # Python-side aggregation: the row count is the windowed runs of one
    # project (bounded), and date bucketing stays out of SQL because
    # CAST(... AS DATE) is not portable across the four dialects (SQLite
    # truncates to the year).
    total_tokens = 0
    users: dict[str, dict] = {}
    conv_ids: set[str] = set()
    daily_tokens: dict[date, int] = defaultdict(int)
    daily_runs: dict[date, int] = defaultdict(int)

    for row in rows:
        delta_val = int(row.delta or 0)
        total_tokens += delta_val
        conv_ids.add(row.conversation_id)

        info = users.setdefault(
            row.user_id,
            {"tokens": 0, "run_count": 0, "conversation_ids": set()},
        )
        info["tokens"] += delta_val
        info["run_count"] += 1
        info["conversation_ids"].add(row.conversation_id)

        started = row.started_at
        if started.tzinfo is None:  # defensive: UTCDateTime re-attaches UTC
            started = started.replace(tzinfo=timezone.utc)
        day = started.date()
        daily_tokens[day] += delta_val
        daily_runs[day] += 1

    # Deterministic ranking: tokens, then run volume, then user_id.
    ranked = sorted(
        users.items(),
        key=lambda kv: (-kv[1]["tokens"], -kv[1]["run_count"], kv[0]),
    )[:_TOP_N]
    top_users = [
        {
            "rank": i + 1,
            "user_id": user_id,
            "tokens": info["tokens"],
            "run_count": info["run_count"],
            "conversation_count": len(info["conversation_ids"]),
        }
        for i, (user_id, info) in enumerate(ranked)
    ]

    daily = [
        {
            "date": (since_date + timedelta(days=i)).isoformat(),
            "tokens": daily_tokens.get(since_date + timedelta(days=i), 0),
            "run_count": daily_runs.get(since_date + timedelta(days=i), 0),
        }
        for i in range(days)
    ]

    return {
        "days": days,
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "summary": {
            "total_tokens": total_tokens,
            "run_count": len(rows),
            "user_count": len(users),
            "conversation_count": len(conv_ids),
        },
        "top_users": top_users,
        "daily": daily,
    }


@router.get("/projects/{project_id}/usage")
async def project_usage(
    project_id: str,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
):
    """Usage summary, top-10 users and daily trend for one project.

    Known accepted imprecisions:
    - A conversation whose first-ever run falls inside the window attributes
      any pre-window lifetime usage to that run (one-time migration-era
      artifact for conversations predating the runs ledger).
    - Delegated (nested) turns write no run row but grow the conversation
      ledger; the next top-level run picks the usage up via the cumulative
      chain — totals converge, bucket time shifts by at most one turn.
    - Soft-deleted conversations keep counting: their tokens were consumed.
    """
    return await _usage_payload(db, str(project.project_id), days)
