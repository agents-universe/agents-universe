"""Agent-as-a-Service publish router.

Two surfaces live here:

- **Management** (SSO-authenticated, ``/api/publishes*``): create/update/
  delete publishes and their API keys. The publisher binds one of their
  model configs (``model_config_id``) — every run, external or embedded,
  executes under that model.
- **Public API** (API-Key-authenticated, ``/api/p/{publish_id}*``): SSE
  conversation stream. A key is required; the key never leaves the request
  header, is never echoed, and is stored only as a SHA-256 hash.

Rate limiting: a process-wide semaphore bounds concurrent published runs so
a burst cannot exhaust the publisher's model quota or the event loop.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models._compat import now_utc as _now_utc
from api.models.agent import Agent
from api.models.project import Project
from api.models.publish import AgentPublish, PublishKey
from api.models.user import UserModelConfig
from api.services.publish import (
    SSEStream,
    generate_publish_key,
    get_or_create_publish_conversation,
    hash_publish_key,
    publish_key_hint,
    sse_format,
)

_log = logging.getLogger("agents_universe.publish")

router = APIRouter()

# Process-wide cap on concurrent published runs (per worker). Bounded, not a
# queue: excess calls 429 immediately instead of stacking up.
_publish_semaphore = asyncio.Semaphore(16)


# ── Pydantic bodies ────────────────────────────────────────────────────────


class PublishCreate(BaseModel):
    agent_slug: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=36)
    model_config_id: str = Field(min_length=1, max_length=36)
    title: str | None = Field(None, max_length=255)
    description: str | None = None


class PublishUpdate(BaseModel):
    title: str | None = Field(None, max_length=255)
    description: str | None = None
    page_enabled: bool | None = None
    api_enabled: bool | None = None
    model_config_id: str | None = Field(None, max_length=36)


class PublishKeyCreate(BaseModel):
    name: str | None = Field(None, max_length=100)


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    # Optional client-supplied thread id — pins a caller's runs to one
    # conversation so multi-turn agents keep their history. Collision-safe:
    # scoped to the publish.
    thread_id: str | None = Field(None, max_length=100)


# ── Auth helpers ───────────────────────────────────────────────────────────


async def _load_owned_publish(db, publish_id: str, user_id: str) -> AgentPublish:
    result = await db.execute(
        select(AgentPublish).where(
            AgentPublish.publish_id == publish_id,
            AgentPublish.owner_id == user_id,
        )
    )
    publish = result.scalar_one_or_none()
    if not publish:
        raise HTTPException(status_code=404, detail="Publish not found")
    return publish


async def _authorize_publish_key(request: Request, db: AsyncSession, publish_id: str):
    """Validate the API key and return the publish (or raise 401/403)."""
    from api.services.publish import authenticate_publish_key

    auth = request.headers.get("authorization") or ""
    api_key = ""
    if auth.lower().startswith("bearer "):
        api_key = auth[7:].strip()
    else:
        # X-API-Key header fallback (some HTTP clients set no Authorization).
        api_key = (request.headers.get("x-api-key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    found = await authenticate_publish_key(db, api_key)
    if found is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    found_pubid, _ = found
    if found_pubid != publish_id:
        raise HTTPException(status_code=401, detail="API key does not match this publish")
    result = await db.execute(
        select(AgentPublish).where(AgentPublish.publish_id == publish_id)
    )
    publish = result.scalar_one_or_none()
    if not publish:
        raise HTTPException(status_code=404, detail="Publish not found")
    if not publish.api_enabled:
        raise HTTPException(status_code=403, detail="Publish API is disabled")
    return publish


# ── Management (SSO) ───────────────────────────────────────────────────────


@router.get("/api/publishes")
async def list_publishes(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(AgentPublish)
        .where(AgentPublish.owner_id == current_user.user_id)
        .order_by(AgentPublish.created_at.desc())
    )
    publishes = result.scalars().all()
    return [
        {
            "publish_id": str(p.publish_id),
            "agent_slug": p.agent_slug,
            "project_id": str(p.project_id),
            "model_config_id": p.model_config_id,
            "title": p.title,
            "description": p.description,
            "page_enabled": p.page_enabled,
            "api_enabled": p.api_enabled,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in publishes
    ]


@router.post("/api/publishes", status_code=201)
async def create_publish(
    body: PublishCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    # Project must exist and the publisher must manage it (creator or
    # whitelisted member) — publishing exposes the project's resources.
    proj_result = await db.execute(
        select(Project).where(
            Project.project_id == body.project_id,
            Project.is_active == True,  # noqa: E712
        )
    )
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    from api.dependencies.auth import is_project_manager
    if not await is_project_manager(db, project, current_user.user_id):
        raise HTTPException(status_code=403, detail="无权发布该项目")

    # Agent must resolve to a global or this-project agent.
    agent_result = await db.execute(
        select(Agent).where(Agent.slug == body.agent_slug)
    )
    agent = agent_result.scalar_one_or_none()
    if agent is None or (agent.project_id is not None and agent.project_id != body.project_id):
        raise HTTPException(status_code=404, detail="Agent not found")

    # Model config must be the publisher's own.
    cfg_result = await db.execute(
        select(UserModelConfig).where(
            UserModelConfig.config_id == body.model_config_id,
            UserModelConfig.user_id == current_user.user_id,
        )
    )
    if cfg_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=400, detail="模型配置不存在或不属于你")

    publish = AgentPublish(
        owner_id=current_user.user_id,
        agent_slug=body.agent_slug,
        project_id=body.project_id,
        model_config_id=body.model_config_id,
        title=body.title,
        description=body.description,
    )
    db.add(publish)
    await db.commit()
    return {
        "publish_id": str(publish.publish_id),
        "agent_slug": publish.agent_slug,
        "project_id": str(publish.project_id),
        "model_config_id": publish.model_config_id,
        "title": publish.title,
        "description": publish.description,
        "page_enabled": publish.page_enabled,
        "api_enabled": publish.api_enabled,
    }


@router.get("/api/publishes/{publish_id}")
async def get_publish(
    publish_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    return {
        "publish_id": str(publish.publish_id),
        "agent_slug": publish.agent_slug,
        "project_id": str(publish.project_id),
        "model_config_id": publish.model_config_id,
        "title": publish.title,
        "description": publish.description,
        "page_enabled": publish.page_enabled,
        "api_enabled": publish.api_enabled,
        "created_at": publish.created_at.isoformat(),
        "updated_at": publish.updated_at.isoformat() if publish.updated_at else None,
    }


@router.patch("/api/publishes/{publish_id}")
async def update_publish(
    publish_id: str,
    body: PublishUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    if body.title is not None:
        publish.title = body.title
    if body.description is not None:
        publish.description = body.description
    if body.page_enabled is not None:
        publish.page_enabled = body.page_enabled
    if body.api_enabled is not None:
        publish.api_enabled = body.api_enabled
    if body.model_config_id is not None:
        cfg_result = await db.execute(
            select(UserModelConfig).where(
                UserModelConfig.config_id == body.model_config_id,
                UserModelConfig.user_id == current_user.user_id,
            )
        )
        if cfg_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=400, detail="模型配置不存在或不属于你")
        publish.model_config_id = body.model_config_id
    publish.updated_at = _now_utc()
    await db.commit()
    return {"publish_id": str(publish.publish_id), "updated": True}


@router.delete("/api/publishes/{publish_id}", status_code=204)
async def delete_publish(
    publish_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    await db.delete(publish)
    await db.commit()
    return None


# ── API keys (SSO) ─────────────────────────────────────────────────────────


@router.post("/api/publishes/{publish_id}/keys", status_code=201)
async def create_publish_key(
    publish_id: str,
    body: PublishKeyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    plaintext = generate_publish_key(publish_id)
    key = PublishKey(
        publish_id=publish.publish_id,
        name=body.name,
        key_hash=hash_publish_key(plaintext),
        key_hint=publish_key_hint(plaintext),
    )
    db.add(key)
    await db.commit()
    # The ONLY time the plaintext is returned — shown once, then discarded.
    return {
        "key_id": str(key.key_id),
        "name": key.name,
        "key": plaintext,
        "key_hint": key.key_hint,
    }


@router.get("/api/publishes/{publish_id}/keys")
async def list_publish_keys(
    publish_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    result = await db.execute(
        select(PublishKey)
        .where(PublishKey.publish_id == publish.publish_id)
        .order_by(PublishKey.created_at.desc())
    )
    keys = result.scalars().all()
    return [
        {
            "key_id": str(k.key_id),
            "name": k.name,
            "key_hint": k.key_hint,
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat(),
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }
        for k in keys
    ]


@router.delete("/api/publishes/{publish_id}/keys/{key_id}", status_code=204)
async def revoke_publish_key(
    publish_id: str,
    key_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    publish = await _load_owned_publish(db, publish_id, current_user.user_id)
    result = await db.execute(
        select(PublishKey).where(
            PublishKey.key_id == key_id,
            PublishKey.publish_id == publish.publish_id,
        )
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    key.is_active = False
    key.revoked_at = _now_utc()
    await db.commit()
    return None


# ── Public API (API Key) ───────────────────────────────────────────────────


@router.post("/api/p/{publish_id}/stream")
async def publish_stream(
    publish_id: str,
    body: RunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """SSE stream for one published-agent turn.

    The request carries the caller's message; the response streams the
    standard agent turn events (``stream_delta``, ``tool_call_start``,
    ``stream_end``, ...) as ``data:`` SSE frames. Runs execute under the
    publisher's bound model config.
    """
    publish = await _authorize_publish_key(request, db, publish_id)
    # Bounded concurrency: a burst of runs must not exhaust the publisher's
    # model quota or the event loop. Fail fast with 429 instead of stacking
    # unbounded tasks when saturated.
    try:
        await asyncio.wait_for(_publish_semaphore.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=429, detail="Too many concurrent runs")

    # Pin the caller's thread to a dedicated conversation so multi-turn
    # agents keep their history across calls.
    thread_suffix = body.thread_id or "default"
    conversation = await get_or_create_publish_conversation(
        db, publish,
        title=f"发布会话: {publish.agent_slug}/{thread_suffix}",
    )

    # Claim the turn so a second concurrent stream on the same conversation
    # fails fast instead of interleaving two runs.
    from api.websocket.manager import manager
    if not await manager.claim_turn(conversation):
        _publish_semaphore.release()
        raise HTTPException(status_code=409, detail="该会话已有一轮运行")

    # SSE paths never open a WS, so no abort event exists (WS turns get one
    # from connect()). Without it, run_turn's abort watcher is never created
    # and an abort request is a silent no-op. Create the event up front and
    # clear any stale set state from a previous turn on this conversation.
    manager.ensure_abort_event(conversation)
    manager.reset_abort(conversation)

    stream = SSEStream()
    from types import SimpleNamespace
    from api.services.agent_turn import run_turn

    async def _run():
        try:
            await run_turn(
                conversation,
                ws=SimpleNamespace(app=request.app),
                msg={"content": body.message, "fixed_config_id": publish.model_config_id},
                user_id=publish.owner_id,
                transport=stream,
                interactive=False,
                actor_user_id=publish.owner_id,
            )
        finally:
            manager.release_turn(conversation)
            _publish_semaphore.release()

    task = asyncio.create_task(_run())

    async def _drain():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(stream.queue.get(), timeout=0.5)
                    yield sse_format(evt)
                    if evt.get("type") == "stream_end":
                        # Terminal frame; the task may still be unwinding.
                        break
                except asyncio.TimeoutError:
                    if task.done():
                        break
            # Flush anything left after the terminal event / task end.
            while not stream.queue.empty():
                try:
                    yield sse_format(stream.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        except asyncio.CancelledError:
            # Client disconnected — stop the run rather than leak it.
            manager.signal_abort(conversation)
            task.cancel()
            raise

    return StreamingResponse(
        _drain(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/p/{publish_id}/abort")
async def publish_abort(
    publish_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Abort the running turn for this publish's default conversation."""
    publish = await _authorize_publish_key(request, db, publish_id)
    conversation = await get_or_create_publish_conversation(db, publish)
    from api.websocket.manager import manager
    manager.signal_abort(conversation)
    return {"aborted": True}


# ── SSO embedded page (viewer-session) ────────────────────────────────────
#
# The embedded page is a link WITHIN the system: the viewer is an
# authenticated user (their session cookie) who opens /p/<id>. Because every
# run executes as the publisher, the shared publish conversation is owned by
# the publisher — the viewer's own cookie alone must not unlock it (the
# conversation belongs to someone else). The page loads via a cookie-authenticated
# GET that issues a short HMAC viewer token (publish, viewer) derived from the
# server secret; the /session paths re-verify it server-side so the conversation
# stays gated on the cookie too.


class SessionLookup(BaseModel):
    token: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=200_000)


class SessionAbort(BaseModel):
    token: str = Field(min_length=8, max_length=64)


@router.get("/api/p/{publish_id}/page")
async def get_publish_page(
    publish_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """First call of the embedded page: payload + a viewer token.

    Cookie-authenticated (no token required yet) — the page issues its own
    viewer-scoped HMAC token for the /session calls that follow. 404 when the
    publish is disabled or missing, so a disabled page is indistinguishable
    from a nonexistent one.
    """
    from api.services.publish import (
        get_or_create_publish_conversation,
        get_publish_viewer_payload,
    )

    # A fresh viewer token can never be pre-approved, so the token check would
    # always fail on this first call. Instead gate purely on page_enabled and
    # existence (the same predicate authorize_publish_viewer enforces).
    from api.models.publish import AgentPublish

    result = await db.execute(
        select(AgentPublish).where(
            AgentPublish.publish_id == publish_id,
            AgentPublish.page_enabled == True,  # noqa: E712
        )
    )
    publish = result.scalar_one_or_none()
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")
    conversation_id = await get_or_create_publish_conversation(db, publish)
    payload = await get_publish_viewer_payload(db, publish, current_user.user_id)
    return {"conversation_id": conversation_id, **payload}


@router.get("/api/p/{publish_id}/session")
async def get_publish_session(
    publish_id: str,
    token: str = Query(min_length=8, max_length=64),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Page payload + resolved conversation for an embedded publish.

    The conversation is created on first page load (like the API path) so the
    viewer's first message finds an active conversation. The ``token`` from
    the page payload is bound to (publish, viewer); only that combination may
    open it.
    """
    from api.services.publish import (
        authorize_publish_viewer,
        get_or_create_publish_conversation,
        get_publish_viewer_payload,
    )

    publish = await authorize_publish_viewer(db, publish_id, current_user.user_id, token)
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")
    conversation_id = await get_or_create_publish_conversation(db, publish)
    payload = await get_publish_viewer_payload(db, publish, current_user.user_id)
    return {"conversation_id": conversation_id, **payload}


@router.get("/api/p/{publish_id}/session/messages")
async def get_publish_session_messages(
    publish_id: str,
    token: str = Query(min_length=8, max_length=64),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Message history for an embedded publish conversation."""
    from api.services.publish import (
        _serialize_publish_messages,
        authorize_publish_viewer,
    )

    publish = await authorize_publish_viewer(db, publish_id, current_user.user_id, token)
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")
    conversation_id = await get_or_create_publish_conversation(db, publish)
    return await _serialize_publish_messages(db, publish, conversation_id)


@router.get("/api/p/{publish_id}/session/runs/latest")
async def get_publish_session_latest_run(
    publish_id: str,
    token: str = Query(min_length=8, max_length=64),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Latest run status for an embedded publish conversation."""
    from api.services.publish import (
        authorize_publish_viewer,
        get_or_create_publish_conversation,
    )

    publish = await authorize_publish_viewer(db, publish_id, current_user.user_id, token)
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")
    conversation_id = await get_or_create_publish_conversation(db, publish)
    from api.services.conversation_runs import latest_run

    run = await latest_run(db, conversation_id)
    if not run:
        return None
    return {
        "run_id": str(run.run_id),
        "status": run.status,
        "user_message_id": run.user_message_id,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "error_message": run.error_message,
        "streaming_snapshot": run.streaming_snapshot,
        "tokens_used": run.tokens_used,
    }


@router.post("/api/p/{publish_id}/session/abort")
async def post_publish_session_abort(
    publish_id: str,
    body: SessionAbort,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Abort the running turn of an embedded publish conversation."""
    from api.services.publish import (
        authorize_publish_viewer,
        get_or_create_publish_conversation,
    )

    publish = await authorize_publish_viewer(db, publish_id, current_user.user_id, body.token)
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")
    conversation_id = await get_or_create_publish_conversation(db, publish)
    from api.websocket.manager import manager

    manager.signal_abort(conversation_id)
    return {"aborted": True}


@router.post("/api/p/{publish_id}/session/run")
async def post_publish_session_run(
    publish_id: str,
    body: SessionLookup,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    """Run a turn of an embedded publish as the publisher (viewer in the box).

    The turn executes under the publisher's bound model config, non-interactive.
    Reuses the same bounded semaphore as the API-key stream so the embedded
    page cannot bypass rate limiting.
    """
    from api.services.publish import (
        SSEStream,
        authorize_publish_viewer,
        get_or_create_publish_conversation,
        sse_format,
    )

    publish = await authorize_publish_viewer(db, publish_id, current_user.user_id, body.token)
    if publish is None:
        raise HTTPException(status_code=404, detail="Publish not found")

    # A duplicate of the publish_stream semaphore guard: the embedded page is
    # rate-limited like any other caller.
    try:
        await asyncio.wait_for(_publish_semaphore.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=429, detail="Too many concurrent runs")

    conversation = await get_or_create_publish_conversation(db, publish)
    from api.websocket.manager import manager

    if not await manager.claim_turn(conversation):
        _publish_semaphore.release()
        raise HTTPException(status_code=409, detail="该会话已有一轮运行")

    # Same abort-event guarantee as the API-key stream: SSE never connects a
    # WS, so the event (and with it run_turn's abort watcher) must exist
    # before the turn starts, or "stop" is a silent no-op.
    manager.ensure_abort_event(conversation)
    manager.reset_abort(conversation)

    stream = SSEStream()
    from types import SimpleNamespace
    from api.services.agent_turn import run_turn

    async def _run():
        try:
            await run_turn(
                conversation,
                ws=SimpleNamespace(app=request.app),
                msg={"content": body.message, "fixed_config_id": publish.model_config_id},
                user_id=publish.owner_id,
                transport=stream,
                interactive=False,
                actor_user_id=publish.owner_id,
            )
        finally:
            manager.release_turn(conversation)
            _publish_semaphore.release()

    task = asyncio.create_task(_run())

    async def _drain():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(stream.queue.get(), timeout=0.5)
                    yield sse_format(evt)
                    if evt.get("type") == "stream_end":
                        break
                except asyncio.TimeoutError:
                    if task.done():
                        break
            while not stream.queue.empty():
                try:
                    yield sse_format(stream.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        except asyncio.CancelledError:
            manager.signal_abort(conversation)
            task.cancel()
            raise

    return StreamingResponse(
        _drain(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
