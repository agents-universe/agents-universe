"""Durable, recoverable hard deletion of project data and workspaces."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models._compat import now_utc
from api.models.agent import Agent
from api.models.conversation import AgentTask, Conversation, Message
from api.models.knowledge import KnowledgeLoadEvent, KnowledgeMetadata, KnowledgeVersion
from api.models.memory import EpisodicMemory, PersonalMemory
from api.models.mcp_server import MCPServer
from api.models.project import Project
from api.models.project_deletion_job import ProjectDeletionJob
from api.models.project_member import ProjectMember
from api.models.project_secret import ProjectSecret
from api.models.script import AutomationScript, ScriptRun
from api.models.task_event import TaskEvent
from api.paths import PROJECTS_ROOT
from api.websocket.manager import manager

_log = logging.getLogger("agents_universe.project_deletion")
_RUNNING = ("pending", "running")
_STATES = ("prepared", "quarantined", "db_deleted", "failed")
_REPARSE = 0x400


class DeletionError(Exception):
    def __init__(self, status: int, code: str, message: str, *, deletion_id: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.deletion_id = deletion_id
        self.retryable = retryable


def _reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & _REPARSE)
    except FileNotFoundError:
        return False


def _safe_paths(slug: str, job_id: str) -> tuple[Path, Path]:
    root = PROJECTS_ROOT.resolve(strict=False)
    if root.exists() and (not root.is_dir() or _reparse_point(root)):
        raise DeletionError(422, "UNSAFE_PATH", "PROJECTS_ROOT is not a real directory")
    if not slug or Path(slug).name != slug or slug in (".", "..") or any(c in slug for c in "\\/"):
        raise DeletionError(422, "UNSAFE_PATH", "Project workspace path is unsafe")
    trash_root = root / ".trash"
    source, trash = root / slug, trash_root / f"{job_id}-{slug}"
    if source.parent != root or trash.parent != trash_root:
        raise DeletionError(422, "UNSAFE_PATH", "Project workspace escapes PROJECTS_ROOT")
    if _reparse_point(source) or _reparse_point(trash_root):
        raise DeletionError(422, "UNSAFE_PATH", "Workspace or trash root is a reparse point")
    return source, trash


def _purge(path: Path) -> None:
    """Reject every link/reparse point before deleting anything."""
    if not path.exists() and not path.is_symlink():
        return
    if _reparse_point(path):
        raise DeletionError(422, "UNSAFE_PATH", "Trash entry is a reparse point")
    if path.is_dir():
        # Walk with scandir so reparse points are rejected before descending;
        # os.walk can already have entered a junction before yielding it.
        pending = [path]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    if _reparse_point(child):
                        raise DeletionError(422, "UNSAFE_PATH", "Workspace contains a reparse point")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
        shutil.rmtree(path)
    else:
        path.unlink()


def _now() -> datetime:
    return now_utc()


async def _check_delete(db: AsyncSession, project: Project) -> None:
    if (await db.execute(select(Project.project_id).where(Project.parent_id == project.project_id).limit(1))).first():
        raise DeletionError(409, "PROJECT_HAS_CHILDREN", "Projects with children cannot be deleted")
    task = await db.execute(
        select(AgentTask.task_id).join(Conversation).where(
            Conversation.project_id == project.project_id,
            Conversation.status == "active",
            AgentTask.status.in_(_RUNNING),
        ).limit(1)
    )
    run = await db.execute(
        select(ScriptRun.run_id).join(AutomationScript).where(
            AutomationScript.project_id == project.project_id, ScriptRun.status.in_(_RUNNING)
        ).limit(1)
    )
    if task.first() or run.first():
        raise DeletionError(409, "PROJECT_HAS_RUNNING_WORK", "Project has pending or running work")
    # In-flight WebSocket agent sessions are not visible in the DB (no row
    # exists until the turn ends) but they hold the workspace open — deleting
    # now would break every subsequent tool call mid-turn. is_turn_active
    # additionally covers the claimed-but-not-yet-registered window (WS frame
    # received, history still loading) that get_running_for_project misses —
    # the same gap delete_conversation/compress close with is_turn_active.
    conv_ids = (await db.execute(
        select(Conversation.conversation_id).where(
            Conversation.project_id == project.project_id,
            Conversation.status == "active",
        )
    )).scalars().all()
    if manager.get_running_for_project(project.project_id) or any(
        manager.is_turn_active(str(cid)) for cid in conv_ids
    ):
        raise DeletionError(409, "PROJECT_HAS_RUNNING_WORK", "Project has an active agent session running")


async def _job_error(db: AsyncSession, job_id: str, exc: Exception, *, failed: bool = True) -> None:
    values = {"error_message": str(exc)[:2000], "last_attempt_at": _now(), "updated_at": _now(), "attempt_count": ProjectDeletionJob.attempt_count + 1}
    if failed:
        values["status"] = "failed"
        values["failed_at"] = _now()
    await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job_id).values(**values))
    await db.commit()


async def delete_project(db: AsyncSession, project_id: str, owner_id: str, confirmation: str) -> None:
    project = (await db.execute(select(Project).where(Project.project_id == project_id).with_for_update())).scalar_one_or_none()
    if project is None or not project.is_active:
        raise DeletionError(404, "PROJECT_NOT_FOUND", "Project not found")
    if project.created_by is None or project.created_by != owner_id:
        raise DeletionError(403, "PROJECT_NOT_OWNER", "Only the project owner can delete it")
    if confirmation != project.slug:
        raise DeletionError(422, "SLUG_CONFIRMATION_MISMATCH", "Confirmation must equal the project slug")
    if project.parent_id is not None:
        raise DeletionError(409, "PROJECT_IS_CHILD", "Child projects cannot be deleted")
    # Close the door for new turns BEFORE _check_delete: the check spans
    # several queries, and a turn claimed in that window (is_active still
    # committed True) would persist messages the deletion transaction then
    # silently drops. authorize_* gates new turns on is_active, so the flip
    # is committed together with the job row below; a failed check reopens
    # the project.
    job_id = str(uuid4())
    source, trash = _safe_paths(project.slug, job_id)
    if trash.exists() or trash.is_symlink():
        raise DeletionError(409, "TRASH_COLLISION", "Trash destination already exists")
    project.is_active = False
    job = ProjectDeletionJob(
        job_id=job_id, project_id=str(project.project_id), slug=project.slug,
        owner_id=owner_id, source_path=str(source), trash_path=str(trash),
        status="prepared", attempt_count=0, prepared_at=_now(),
    )
    db.add(job)
    await db.commit()  # flip + prepare are durable before touching the filesystem

    try:
        await _check_delete(db, project)
    except BaseException as exc:
        # Running work refuses the deletion — reopen the project (the flip
        # would otherwise freeze it) and record the refusal on the job row.
        await db.rollback()
        await db.execute(update(Project).where(Project.project_id == project_id).values(is_active=True))
        await _job_error(db, job_id, exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
        raise

    quarantined = False
    try:
        if source.exists() or source.is_symlink():
            if _reparse_point(source):
                raise DeletionError(422, "UNSAFE_PATH", "Workspace is a reparse point")
            trash.parent.mkdir(parents=True, exist_ok=True)
            if _reparse_point(trash.parent):
                raise DeletionError(422, "UNSAFE_PATH", "Trash root is a reparse point")
            # rename is synchronous filesystem work — a GB-scale workspace
            # rename must not stall the event loop (all WS streams / HTTP).
            await asyncio.to_thread(source.rename, trash)
            quarantined = True
        await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job_id).values(
            status="quarantined", quarantined_at=_now(), last_attempt_at=_now(),
        ))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if quarantined:
            try:
                if not source.exists() and trash.exists() and not _reparse_point(trash):
                    await asyncio.to_thread(trash.rename, source)
            except Exception:
                _log.exception("Could not restore workspace for deletion job %s", job_id)
        # The workspace is either untouched (failure happened before the
        # rename) or restored above — reopen the project. delete_project
        # flipped is_active off to close the TOCTOU window; a failed
        # deletion must not freeze the project forever. When the workspace
        # is truly gone (moved to trash and the restore itself failed) the
        # update is skipped — reopening a project without its workspace
        # would be worse.
        if source.exists() or source.is_symlink():
            await db.execute(update(Project).where(Project.project_id == project_id).values(is_active=True))
        await _job_error(db, job_id, exc)
        raise exc if isinstance(exc, DeletionError) else DeletionError(503, "PROJECT_DELETE_PENDING", "Project deletion is pending cleanup", deletion_id=job_id, retryable=True) from exc

    try:
        # Delete leaf tables first. The job is updated in the same transaction as
        # the physical project delete, so db_deleted is durable and truthful.
        async with db.begin():
            conversations = select(Conversation.conversation_id).where(Conversation.project_id == project_id)
            knowledge = select(KnowledgeMetadata.knowledge_id).where(KnowledgeMetadata.project_id == project_id)
            scripts = select(AutomationScript.script_id).where(AutomationScript.project_id == project_id)
            await db.execute(delete(KnowledgeLoadEvent).where(
                KnowledgeLoadEvent.conversation_id.in_(conversations) | KnowledgeLoadEvent.knowledge_id.in_(knowledge)
            ))
            await db.execute(delete(TaskEvent).where(TaskEvent.conversation_id.in_(conversations)))
            await db.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
            await db.execute(update(AgentTask).where(AgentTask.conversation_id.in_(conversations)).values(parent_task_id=None))
            await db.execute(delete(AgentTask).where(AgentTask.conversation_id.in_(conversations)))
            await db.execute(delete(EpisodicMemory).where(EpisodicMemory.project_id == project_id))
            await db.execute(delete(Conversation).where(Conversation.project_id == project_id))
            await db.execute(delete(KnowledgeVersion).where(KnowledgeVersion.knowledge_id.in_(knowledge)))
            await db.execute(delete(KnowledgeMetadata).where(KnowledgeMetadata.project_id == project_id))
            await db.execute(delete(PersonalMemory).where(PersonalMemory.project_id == project_id))
            await db.execute(delete(ScriptRun).where(ScriptRun.script_id.in_(scripts)))
            await db.execute(delete(AutomationScript).where(AutomationScript.project_id == project_id))
            await db.execute(delete(ProjectSecret).where(ProjectSecret.project_id == project_id))
            await db.execute(delete(MCPServer).where(MCPServer.project_id == project_id))
            await db.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id))
            await db.execute(delete(Agent).where(Agent.project_id == project_id))
            await db.execute(delete(Project).where(Project.project_id == project_id))
            await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job_id).values(
                status="db_deleted", db_deleted_at=_now(), last_attempt_at=_now(), attempt_count=ProjectDeletionJob.attempt_count + 1,
            ))
    except Exception as exc:
        await db.rollback()
        # Only a failed DB transaction with the authoritative Project still
        # present may restore the quarantined workspace.
        still_exists = (await db.execute(select(Project.project_id).where(Project.project_id == project_id))).first()
        if still_exists and quarantined:
            try:
                if not source.exists() and trash.exists() and not _reparse_point(trash):
                    await asyncio.to_thread(trash.rename, source)
                    # Workspace restored — reopen the project (see the
                    # rename-failure recovery branch).
                    await db.execute(update(Project).where(Project.project_id == project_id).values(is_active=True))
            except Exception:
                _log.exception("Could not restore workspace for deletion job %s", job_id)
        elif still_exists:
            # The workspace was never touched (no workspace directory
            # existed) — nothing was quarantined, so reopening the project
            # carries no risk. Without this the project would stay frozen
            # forever with no recovery path.
            await db.execute(update(Project).where(Project.project_id == project_id).values(is_active=True))
        await _job_error(db, job_id, exc)
        raise DeletionError(503, "PROJECT_DELETE_PENDING", "Project deletion is pending cleanup", deletion_id=job_id, retryable=True) from exc

    try:
        # _purge walks and rmtree's the whole workspace tree — run it off the
        # event loop or a big project would stall every connection.
        await asyncio.to_thread(_purge, trash)
    except Exception as exc:
        # DB deletion is already durable. Keep the db_deleted phase and trash;
        # changing it to failed would obscure that the source is gone.
        await _job_error(db, job_id, exc, failed=False)
        raise DeletionError(503, "PROJECT_DELETE_PENDING", "Project deletion is pending cleanup", deletion_id=job_id, retryable=True) from exc
    await db.execute(delete(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job_id))
    await db.commit()


async def startup_sweep(db: AsyncSession) -> None:
    """Retry only durable jobs; never infer deletion from a reused slug."""
    jobs = (await db.execute(select(ProjectDeletionJob).where(ProjectDeletionJob.status.in_(_STATES)))).scalars().all()
    for job in jobs:
        try:
            source, trash = _safe_paths(job.slug, job.job_id)
            project_exists = (await db.execute(select(Project.project_id).where(Project.project_id == job.project_id))).first() is not None
            if project_exists:
                # A prepared job has not quarantined anything yet, but the
                # process may have died between the is_active=False commit
                # and the rename — the project would stay frozen forever
                # otherwise. Restore whichever state the deletion left
                # behind, then mark the job failed for audit (never silently
                # dropped).
                if source.exists() or source.is_symlink():
                    # Workspace never moved (crash before the rename) — the
                    # project is fully intact, just reopen it.
                    await db.execute(update(Project).where(Project.project_id == job.project_id).values(is_active=True))
                    await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job.job_id).values(status="failed", last_attempt_at=_now(), attempt_count=ProjectDeletionJob.attempt_count + 1))
                    await db.commit()
                elif trash.exists() and not source.exists():
                    # Crash after the rename: restore the workspace and
                    # reopen the project (a deletion job that flipped
                    # is_active off failed before finishing).
                    if _reparse_point(trash):
                        raise DeletionError(422, "UNSAFE_PATH", "Trash entry is a reparse point")
                    await asyncio.to_thread(trash.rename, source)
                    await db.execute(update(Project).where(Project.project_id == job.project_id).values(is_active=True))
                    await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job.job_id).values(status="failed", last_attempt_at=_now(), attempt_count=ProjectDeletionJob.attempt_count + 1))
                    await db.commit()
                else:
                    # No workspace directory ever existed — the DB transaction
                    # failed before touching the filesystem. Reopen the project
                    # (never frozen by a failed deletion).
                    await db.execute(update(Project).where(Project.project_id == job.project_id).values(is_active=True))
                    await db.execute(update(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job.job_id).values(status="failed", last_attempt_at=_now(), attempt_count=ProjectDeletionJob.attempt_count + 1))
                    await db.commit()
                continue
            # A missing project is safe to purge only after DB deletion was (or
            # could have been) reached. Source is never touched in this branch.
            if job.status == "db_deleted":
                await asyncio.to_thread(_purge, trash)
                await db.execute(delete(ProjectDeletionJob).where(ProjectDeletionJob.job_id == job.job_id))
                await db.commit()
        except Exception as exc:
            await db.rollback()
            _log.error("Project deletion cleanup pending for job %s: %s", job.job_id, exc, exc_info=True)
