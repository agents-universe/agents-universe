"""WebSocket handlers for conversation streaming."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


from .manager import manager
from ..services.agent_turn import (  # noqa: F401  (re-export for WS watchdog + tests)
    _load_history,
    _load_active_task_plan,
    _prepare_and_persist_user_message,
    _persist_assistant_message,
    _persist_terminal_task_event,
    _update_task_status,
    _upsert_file_records,
    _prepare_attachment,
    _validate_attachment_url,
    _save_secret_from_response,
    _save_user_token_from_response,
    _enqueue_injected_message,
    _guard_injected_message,
    _persist_orphan_injection,
    _MAX_INLINE_TEXT_CHARS,
    _REHYDRATE_IMAGE_TURNS,
)

_log = logging.getLogger("agents_universe.ws")

router = APIRouter()

# Strong references for fire-and-forget background tasks - the event loop
# keeps only weak references, so an unreferenced task can be garbage
# collected mid-await. The set self-cleans on completion.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _log_unexpected(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            _log.error("Background task failed: %s", exc, exc_info=exc)

    task.add_done_callback(_log_unexpected)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _check_project_access(conversation_id: str, user_id: str) -> bool:
    """Whether user_id may still interact with the conversation's project.

    Private projects require creator/whitelisted-member status. Used by the
    WS main loop for paths _handle_message's per-turn check never reaches
    (in-flight injections, user_selection_response secret saves): a socket
    opened before access was revoked must not keep writing project data.
    """
    from api.database import AsyncSessionLocal
    from api.dependencies.auth import has_project_access
    from api.models.conversation import Conversation
    from api.models.project import Project
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Conversation, Project)
                .join(Project, Project.project_id == Conversation.project_id)
                .where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                )
            )
        ).first()
        if not row:
            return False
        _, project = row
        return await has_project_access(db, project, user_id)


@router.websocket("/ws/conversations/{conversation_id}")
async def conversation_ws(conversation_id: str, ws: WebSocket):
    """WebSocket endpoint for a conversation.

    Authenticates via the httpOnly 'x-auth-token' cookie (sent automatically
    by the browser on same-origin requests).

    Message types FROM client:
      {"type": "message", "content": "...", "agent_id": "...",
       "attachments": [{"url", "name", "media_type", "size"}, ...]}
      {"type": "abort"}
      {"type": "ping"}
      {"type": "user_selection_response", "prompt_id": "...", "value": "..."}

    Message types TO client:
      stream_delta, tool_call_start, tool_call_end, knowledge_loaded,
      token_update, image_output, stream_end, error,
      complexity_assessed, context_usage, task_plan_created, task_started,
      task_progress, task_completed, task_failed, task_plan_revised,
      agentic_loop_completed, abort_ack
    """
    from api.config import get_settings
    from api.services.redis_client import _get_pool, get_session, save_session

    settings = get_settings()

    if settings.auth_bypass_enabled:
        user_id = settings.auth_bypass_user_id
    else:
        session_id = ws.cookies.get(settings.auth_cookie_name)
        if not session_id:
            await ws.close(code=4001)
            return
        try:
            redis = _get_pool()
            session_data = await get_session(redis, session_id)
        except Exception:
            _log.warning("WebSocket session auth failed for conversation %s", conversation_id, exc_info=True)
            await ws.close(code=4001)
            return
        if not session_data or "user_id" not in session_data:
            await ws.close(code=4001)
            return
        user_id = session_data["user_id"]

    # Authorize the conversation before registering the socket.  In particular,
    # do not let an unauthorized client replace the connection/abort event for
    # a conversation key it does not own. Also re-check project visibility at
    # connect: a member removed from a private project must not reconnect into
    # the socket (the per-message check below backstops sockets that were
    # already open when access was revoked).
    from api.database import AsyncSessionLocal
    from api.dependencies.auth import has_project_access
    from api.models.conversation import Conversation
    from api.models.project import Project
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(Conversation, Project)
                .join(Project, Project.project_id == Conversation.project_id)
                .where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.status == "active",
                    Project.is_active == True,  # noqa: E712
                )
            )
        ).first()
        denied_reason: str | None = None
        if row:
            _, project = row
            if not await has_project_access(db, project, user_id):
                denied_reason = "该项目为私有项目，您没有访问权限"

    if row is None or denied_reason:
        # Accept only long enough to return a protocol-level error; manager.connect
        # must never run on this path, so abort/selection messages cannot affect the
        # owner's active connection.
        await ws.accept()
        await ws.send_json({"type": "error", "message": denied_reason or "Conversation not found"})
        await ws.close(code=4403)
        return

    await manager.connect(conversation_id, ws)

    # If an agent session is already running (e.g. user switched away and
    # came back), send a sync event so the client can restore streaming
    # state before receiving new events. This send happens BEFORE the main
    # loop's try block — a disconnect here (client closed mid-handshake)
    # would otherwise skip the except WebSocketDisconnect cleanup and leave
    # a dead socket pinned in manager._connections with its _abort_events
    # and _session_memories. Clean up explicitly and bail.
    existing_session = manager.get_session(conversation_id)
    if existing_session:
        try:
            await ws.send_json({
                "type": "sync",
                "streaming_text": existing_session.current_streaming_text,
                "tool_calls": existing_session.current_tool_calls,
            })
        except Exception:
            await manager.disconnect(conversation_id, ws)
            return

    active_task: asyncio.Task | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue
            # Legal JSON that is not an object ([] / "str" / 42 / null) would
            # crash at msg.get() below and, via the outer except Exception,
            # cancel the running agent turn — reject it like malformed JSON.
            if not isinstance(msg, dict):
                await ws.send_json({"type": "error", "message": "Invalid message format"})
                continue

            if msg.get("type") == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if msg.get("type") == "abort":
                manager.signal_abort(conversation_id)
                continue

            # Re-check access on every message that touches project state.
            # The connect-time check covers socket opening and _handle_message
            # covers new turns — but in-flight injections and
            # user_selection_response (secret saves included) never reach
            # _handle_message, so a socket opened before a project flipped
            # private (or a member was removed) would keep writing project
            # data. Fail closed on the next interaction. ping/abort stay
            # reachable so a cut-off socket can still end its running turn.
            if msg.get("type") in ("message", "user_selection_response"):
                if not await _check_project_access(conversation_id, user_id):
                    await ws.send_json({"type": "error", "message": "该项目为私有项目，您没有访问权限"})
                    continue

            if msg.get("type") == "user_selection_response":
                prompt_id = msg.get("prompt_id") or msg.get("request_id", "")
                is_secret = msg.get("secret", False)
                save_to_project = msg.get("save_to_project_secrets", False)
                save_to_user = msg.get("save_to_user_tokens", False)

                # Always persist secrets even if the agent session has ended
                # (e.g. task timed out while waiting for user input).
                if is_secret and save_to_project:
                    saved = await _save_secret_from_response(
                        conversation_id, user_id, msg
                    )
                    _log.info("user_selection_response: saved secret=%s prompt_id=%s", saved, prompt_id)
                    if saved:
                        await ws.send_json({"type": "secrets_updated"})
                    sess = manager.get_session(conversation_id)
                    if sess:
                        sess.resolve_user_selection_secret(prompt_id, saved=saved)
                    else:
                        # Agent already finished; secret is saved for next invocation
                        await ws.send_json({"type": "info", "message": "密钥已保存。下次调用时将自动使用。"})
                elif is_secret and save_to_user:
                    saved = await _save_user_token_from_response(
                        user_id, msg
                    )
                    _log.info("user_selection_response: saved user token=%s prompt_id=%s", saved, prompt_id)
                    if saved:
                        await ws.send_json({"type": "user_tokens_updated"})
                    sess = manager.get_session(conversation_id)
                    if sess:
                        sess.resolve_user_selection_secret(prompt_id, saved=saved)
                    else:
                        await ws.send_json({"type": "info", "message": "密钥已保存到用户仓库。下次调用时将自动使用。"})
                else:
                    if is_secret:
                        # A secret response frame without a save_to_* flag
                        # must never be resolved as a plain selection — that
                        # would put the client-supplied secret value into the
                        # agent's message history. Refuse instead.
                        _log.warning(
                            "user_selection_response: secret response without save_to_* flag, prompt_id=%s — value ignored",
                            prompt_id,
                        )
                        await ws.send_json({
                            "type": "error",
                            "message": "Secret response missing save_to_project_secrets/save_to_user_tokens — nothing saved, value not forwarded.",
                        })
                        continue
                    sess = manager.get_session(conversation_id)
                    if sess:
                        resolved = sess.resolve_user_selection(
                            prompt_id, msg.get("value", "")
                        )
                        if not resolved:
                            _log.warning("user_selection_response: no pending prompt for prompt_id=%s", prompt_id)
                            await ws.send_json({"type": "error", "message": "No pending prompt for that prompt_id"})
                    else:
                        _log.warning("user_selection_response: no session for %s", conversation_id)
                        await ws.send_json({"type": "error", "message": "No active agent session — the agent may have timed out. Please resend your message."})
                continue

            if msg.get("type") == "message":
                # New message while an agent turn is running (local task, a
                # background session from a previous WS connection, or a turn
                # claimed by a racing second connection) becomes an in-flight
                # injection: queued, consumed at the agent's next step
                # boundary — never interrupting the running LLM call or tool.
                if (
                    (active_task and not active_task.done())
                    or manager.is_session_active(conversation_id)
                    or not await manager.claim_turn(conversation_id)
                ):
                    content = msg.get("content", "")
                    attachments = msg.get("attachments") or []
                    # Same bounds the persist path enforces — an oversized
                    # message must not be queued for the agent. attachments
                    # must be a list too: a str/dict passes len() and later
                    # AttributeErrors inside attachment validation.
                    if not isinstance(content, str) or len(content) > 200_000:
                        # input_rejected (not "error"): the turn keeps streaming
                        # and only the optimistic pending entry is settled — an
                        # "error" event would clear the streaming state and pin
                        # a permanent error message onto a running turn.
                        await ws.send_json({"type": "input_rejected", "message_id": None, "content": content, "message": "Message content exceeds the 200,000 character limit"})
                        continue
                    if not isinstance(attachments, list):
                        await ws.send_json({"type": "input_rejected", "message_id": None, "content": content, "message": "Message carries invalid attachments"})
                        continue
                    if len(attachments) > 10:
                        await ws.send_json({"type": "input_rejected", "message_id": None, "content": content, "message": "Message carries more than 10 attachments"})
                        continue
                    if not content.strip() and not attachments:
                        # An empty message would persist a blank user row and
                        # run a full agent turn on nothing. Pure-attachment
                        # messages stay legal.
                        await ws.send_json({"type": "input_rejected", "message_id": None, "content": content, "message": "Message is empty"})
                        continue
                    sess = manager.get_session(conversation_id)
                    if sess is None:
                        # Turn claimed but session not yet registered (the
                        # history-load window) — buffer it; _handle_message
                        # drains into the session after register_session.
                        if not manager.enqueue_pending_injection(conversation_id, msg):
                            await ws.send_json({"type": "input_rejected", "message_id": None, "content": content, "message": "Input queue is full. Wait for the agent to finish."})
                            continue
                        await ws.send_json({"type": "input_queued", "message_id": None, "content": content})
                        continue
                    await _enqueue_injected_message(conversation_id, sess, msg)
                    continue
                try:
                    # Reset abort event before processing new message
                    manager.reset_abort(conversation_id)
                except Exception:
                    # The claim is only released by _handle_message's finally —
                    # if the task is never created, the conversation would
                    # stay claimed forever, rejecting every later message.
                    manager.release_turn(conversation_id)
                    raise
                # Renew session TTL on each user message — best-effort like
                # the REST side (renew_session_ttl): a transient Redis
                # failure must not drop the user's message or kill the
                # connection, so the session entry simply ages out sooner.
                if not settings.auth_bypass_enabled:
                    try:
                        await save_session(redis, session_id, session_data, settings.session_ttl)
                    except Exception:
                        _log.warning(
                            "save_session failed for session %s (continuing)",
                            session_id, exc_info=True,
                        )
                active_task = asyncio.create_task(
                    _handle_message(conversation_id, ws, msg, user_id)
                )
                active_task.add_done_callback(lambda t: _on_task_done(t, ws))

    except WebSocketDisconnect:
        # Do NOT abort the agent - it may still be running.  The agent's
        # forward_events() continues persisting to DB; manager.send()
        # silently drops live events when no WS is connected.  When the
        # user returns, a new WS reconnects and picks up the live stream.
        if active_task and not active_task.done():
            _log.debug(
                "WS disconnected but agent still running for %s - continuing in background",
                conversation_id,
            )
        elif not manager.is_turn_active(conversation_id):
            # A PASSIVE connection (never sent a message) has no local
            # active_task even while the background agent is still running —
            # the session stays registered until the turn finishes. Without
            # the is_turn_active guard, disconnecting such a connection would
            # snapshot a PARTIAL conversation into episodic memories, and the
            # existing-episode early-return in episodic_service would then
            # permanently block the complete summary.
            _spawn_background(_maybe_generate_episode(conversation_id, user_id))
        await manager.disconnect(conversation_id, ws)
    except Exception as e:
        _log.error("WebSocket handler error for %s: %s", conversation_id, e, exc_info=True)
        try:
            await ws.send_json({"type": "error", "message": "An internal error occurred. Please try again."})
        except Exception:
            _log.debug("Failed to send error to client for %s (likely disconnected)", conversation_id)
        # Do NOT cancel active_task: the agent may still be running and keeps
        # persisting via forward_events — mirroring the WebSocketDisconnect
        # path above. A receive-loop error (e.g. a transient DB failure in
        # _check_project_access) must not kill a healthy turn; the user can
        # still abort through a reconnect (abort frame → signal_abort), and
        # manager.send() tolerates the missing socket.
        await manager.disconnect(conversation_id, ws)


def _on_task_done(task: asyncio.Task, ws: WebSocket) -> None:
    """Log unhandled exceptions from background agent tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        import logging
        logging.getLogger("agents_universe.ws").error(
            "Agent task failed (unhandled): %s", exc, exc_info=exc
        )


# --- In-flight user input injection -----------------------------------------
#
# A message sent while the agent is running is queued on the session and
# consumed at the agent's next step boundary. The enqueue / watchdog /
# orphan-persist helpers live in services/agent_turn.py (shared by the WS
# receive loop here and the run_turn claim-window drain) and are imported at
# the top of this module.

async def _handle_message(
    conversation_id: str, ws: WebSocket, msg: dict, user_id: str
) -> None:
    """Run agent for the incoming user message and stream events back.

    Thin shell over the shared turn kernel (services/agent_turn.py). The
    conversation-lookup, access gate, model selection, event persistence and
    session lifecycle live there; this WS handler keeps only its own
    pre/post-turn transport wiring.
    """
    from ..services.agent_turn import run_turn
    await run_turn(conversation_id, ws, msg, user_id)


async def _maybe_generate_episode(conversation_id: str, user_id: str) -> None:
    """Generate an episodic memory summary if the conversation is long enough."""
    try:
        from api.services.episodic_service import generate_episodic_summary
        await generate_episodic_summary(conversation_id, user_id)
    except Exception as e:
        _log.warning("Episodic generation failed for %s: %s", conversation_id, e)
