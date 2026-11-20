"""Per-user running-turn counts for the sidebar badges (best-effort, never raise).

A "running turn" matches the conversation-tree green dot semantics: a
`conversation_runs` row still `running` UNION an in-memory active session
(`ws_manager.get_running_conversations`) — the union covers the sub-second
claim→create_run window and a session outliving `finish_run` by a beat.

Counts are computed with O(1) queries (one grouped aggregate + at most one
tiny membership check) and pushed to `/ws/user` sockets as a full snapshot
under a per-user debounce, so clients never refetch and bursts of multi-turn
transitions coalesce into a single frame.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, or_, select

from api.database import AsyncSessionLocal
from api.models.agent import Agent
from api.models.conversation import Conversation, Message as DbMessage
from api.models.conversation_run import ConversationRun
from api.models.project import Project
from api.models.project_member import ProjectMember
from api.websocket.manager import manager as ws_manager

_log = logging.getLogger("agents_universe.running_counts")

# Trailing debounce per user: a multi-task turn flips create/finish rapidly;
# one frame per quiet window instead of one per transition.
_DEBOUNCE_S = 0.3

# Strong references for pending debounce timers (same rationale as
# handlers._background_tasks — the loop keeps only weak refs).
_pending: dict[str, asyncio.Task] = {}


async def compute_running_counts(user_id: str) -> dict:
    """Aggregate running turns for *user_id* into {by_project, by_agent}.

    Keys with count < 1 are omitted; callers treat missing keys as 0.
    """
    mem_ids = ws_manager.get_running_conversations(user_id)

    async with AsyncSessionLocal() as db:
        agent_key = func.coalesce(DbMessage.agent_slug, Agent.slug)
        visibility = or_(
            Project.visibility != "private",
            Project.created_by == user_id,
            select(ProjectMember.project_id)
            .where(
                ProjectMember.project_id == Project.project_id,
                ProjectMember.user_id == user_id,
            )
            .exists(),
        )
        scope = [
            Conversation.user_id == user_id,
            Conversation.status == "active",
            or_(Conversation.source.is_(None), Conversation.source != "publish"),
            Project.is_active == True,  # noqa: E712
            visibility,
        ]

        # Primary aggregate: conversations with a `running` run row — the
        # hot path, one grouped query.
        rows = (
            await db.execute(
                select(
                    Conversation.project_id,
                    agent_key.label("agent_key"),
                    func.count(func.distinct(Conversation.conversation_id)).label("n"),
                )
                .select_from(ConversationRun)
                .join(
                    Conversation,
                    Conversation.conversation_id == ConversationRun.conversation_id,
                )
                .join(Project, Project.project_id == Conversation.project_id)
                .outerjoin(
                    DbMessage, DbMessage.message_id == ConversationRun.user_message_id
                )
                .outerjoin(Agent, Agent.agent_id == Conversation.agent_id)
                .where(ConversationRun.status == "running", *scope)
                .group_by(Conversation.project_id, agent_key)
            )
        ).all()

        # In-memory sessions with NO running run row (the claim→create_run
        # window, or a create_run that failed best-effort). Excluded from the
        # aggregate above, so no double-count. A conversation with zero run
        # rows is invisible to a FROM conversation_runs join — hence a second
        # tiny query; mem_ids is O(active turns for this user), never O(table).
        mem_only: set[str] = set()
        if mem_ids:
            covered = await db.execute(
                select(ConversationRun.conversation_id).where(
                    ConversationRun.status == "running",
                    ConversationRun.conversation_id.in_(mem_ids),
                )
            )
            mem_only = mem_ids - set(covered.scalars())
        if mem_only:
            # Attribution falls back to the conversation's default agent —
            # without a run row there is no user_message_id to read a mention
            # slug from (sub-second window; the next broadcast corrects it).
            rows += (
                await db.execute(
                    select(
                        Conversation.project_id,
                        Agent.slug.label("agent_key"),
                        func.count(func.distinct(Conversation.conversation_id)).label("n"),
                    )
                    .select_from(Conversation)
                    .join(Project, Project.project_id == Conversation.project_id)
                    .outerjoin(Agent, Agent.agent_id == Conversation.agent_id)
                    .where(Conversation.conversation_id.in_(mem_only), *scope)
                    .group_by(Conversation.project_id, Agent.slug)
                )
            ).all()

    by_project: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    for project_id, agent_slug, n in rows:
        count = int(n)
        if count < 1:
            continue
        by_project[str(project_id)] = by_project.get(str(project_id), 0) + count
        if agent_slug:
            by_agent[str(agent_slug)] = by_agent.get(str(agent_slug), 0) + count
    return {"by_project": by_project, "by_agent": by_agent}


async def _broadcast(user_id: str) -> None:
    try:
        counts = await compute_running_counts(user_id)
        await ws_manager.send(
            f"user:{user_id}", {"type": "running_counts", **counts}
        )
    except Exception:
        _log.warning("running-counts broadcast failed for %s", user_id, exc_info=True)


async def _debounced(user_id: str) -> None:
    try:
        await asyncio.sleep(_DEBOUNCE_S)
        await _broadcast(user_id)
    finally:
        _pending.pop(user_id, None)


async def notify_conversation_running_changed(
    conversation_id: str,
) -> asyncio.Task | None:
    """Schedule a debounced counts push for the conversation's owner.

    Best-effort: never raises (callers are `create_run`/`finish_run`, whose
    module contract is never-raise). Returns the scheduled task so tests can
    await it deterministically.
    """
    try:
        async with AsyncSessionLocal() as db:
            owner = (
                await db.execute(
                    select(Conversation.user_id).where(
                        Conversation.conversation_id == conversation_id
                    )
                )
            ).scalar_one_or_none()
        if not owner:
            return None
        task = _pending.pop(owner, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        task = asyncio.create_task(_debounced(owner))
        _pending[owner] = task
        return task
    except Exception:
        _log.warning(
            "notify_conversation_running_changed failed for %s",
            conversation_id,
            exc_info=True,
        )
        return None
