"""Agent turn execution — the shared, transport-agnostic core.

``run_turn`` runs one agent turn (credential loading, context building,
``agent.run()`` + event persistence) and forwards session events to a
*transport*. The WebSocket handler calls it with the WS manager as transport
(broadcast to the live socket, tolerant of a dead connection); the published-
agent SSE path will call it with an SSE event stream as transport.

The extracted turn replaces the old inline body of ``_handle_message``; the
caller owns the conversation-lookup and access gate that used to precede it
(nothing here touches auth — the handler's per-turn checks stay put).

Signature notes:

- ``ws`` is kept positional (third arg) so the existing tests that drive
  ``_handle_message(conversation_id, _ws(), msg, "test-user")`` pass
  unchanged. It is only used for ``ws.app.state`` (process registries) and as
  a last-resort send target when the transport has no live connection.
- ``transport`` is an object with ``send(conversation_id, data) -> bool``
  (the manager doubles as one) or None for the plain WS path.
- ``interactive`` gates human-interaction tools: a headless published run
  must degrade to a readable error instead of blocking on a prompt.
- ``actor_user_id`` is the user whose per-conversation history is read and
  written on this turn. It differs from ``user_id`` only on the SSO published
  page path, where the viewer owns the conversation but the run executes as
  the publisher (credentials + ownership). Defaults to ``user_id``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid as _uuid_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import correlation_ctx_var, request_id_var
from ..websocket.manager import manager

_log = logging.getLogger("agents_universe.ws")

# Reserved config_id for the composer's "auto" option: routes each turn by
# task complexity across the user's tiered model configs. Resolved below to a
# real config_id before provider_override, so the literal never reaches
# agent.run().
_AUTO_CONFIG_ID = "auto"

# Strong references for fire-and-forget background tasks — the event loop
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


class Transport:
    """Minimal send contract for forwarding session events to a client.

    ``send`` must return True when the event was delivered, False when no
    live connection exists (or delivery failed) — the turn persists events to
    the DB either way.
    """

    async def send(self, conversation_id: str, data: dict) -> bool:
        raise NotImplementedError


# The ConnectionManager already satisfies the send contract; a small adapter
# keeps the turn code transport-agnostic without touching manager internals.
class _ManagerTransport(Transport):
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, conversation_id: str, data: dict) -> bool:
        delivered = await manager.send(conversation_id, data)
        # manager.send targets the live connection. With nobody connected
        # (the test path drives _handle_message with no socket), fall back to
        # the raw socket like the old _send_turn_error did so errors are not
        # silently lost on the last-resort path.
        if not delivered:
            try:
                await self._ws.send_json(data)
                return True
            except Exception:
                return False
        return True


async def _send_turn_error(transport: Transport | None, conversation_id: str, data: dict) -> None:
    """Send a turn-level error to the CURRENT connection for this conversation.

    Early validation errors are emitted before the agent turn starts. If the
    user disconnected and reconnected meanwhile, the socket that received the
    message is dead and the error would be lost — the UI on the new connection
    would stay "processing" forever. The transport targets the live connection
    (or returns False when nobody is connected).
    """
    if transport is None:
        return
    await transport.send(conversation_id, data)


async def run_turn(
    conversation_id: str,
    ws: Any,
    msg: dict,
    user_id: str,
    *,
    transport: Transport | None = None,
    interactive: bool = True,
    actor_user_id: str | None = None,
) -> None:
    """Run the agent for the incoming user message and stream events back."""
    from api.config import get_settings
    from api.database import AsyncSessionLocal
    from agent_core.agent import Agent, AgentConfig
    from agent_core.compressor import estimate_history_tokens
    from agent_core.knowledge.loader import load_project_context
    from agent_core.model_routing import cheapest_tier, resolve_tier_config
    from agent_core.session import ConversationSession
    from agent_core.skills.registry import SkillRegistry
    from agent_core.tools.base import ToolContext
    from sqlalchemy import select
    from api.models.conversation import Conversation
    from api.models.project import Project

    # Plain WS path (no explicit transport): resolve the manager adapter now
    # so every downstream push is transport-agnostic. The adapter broadcasts
    # to the live socket and tolerates a dropped connection.
    if transport is None:
        transport = _ManagerTransport(ws)

    settings = get_settings()

    # The turn's owner: owns the conversation row and (on the SSO published
    # path) the session history. Defaults to the caller — the WS path.
    turn_user_id = actor_user_id or user_id

    # Uploads made before this moment are this turn's consumed attachments and
    # are released on exit; uploads arriving DURING the turn (user attaches a
    # file while the agent runs) must survive for the next send.
    turn_started = time.time()

    # False until manager.register_session runs — a failure before that point
    # leaves the claim-window buffer belonging to a dead turn (see finally).
    session_registered = False

    req_token = request_id_var.set(str(_uuid_mod.uuid4())[:8])
    ctx_token = correlation_ctx_var.set({
        "conversation_id": conversation_id,
        "user_id": user_id,
    })

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Conversation, Project)
                .join(Project, Project.project_id == Conversation.project_id)
                .where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == turn_user_id,
                    Conversation.status == "active",
                    Project.is_active == True,  # noqa: E712
                )
            )
            row = result.first()
            if not row:
                await _send_turn_error(transport, conversation_id, {"type": "error", "message": "Conversation not found"})
                return
            conv, project = row

            project_id = str(conv.project_id)
            correlation_ctx_var.set({
                "conversation_id": conversation_id,
                "project_id": project_id,
                "user_id": user_id,
            })

            # The conversation's default agent (Conversation.agent_id -> slug).
            # An @-mention turn runs a different agent while the default stays
            # unchanged - the delta drives the history annotation and the
            # system note below.
            conv_default_agent_slug: str | None = None
            if conv.agent_id:
                from api.models.agent import Agent as AgentModel
                _da = await db.execute(
                    select(AgentModel.slug).where(AgentModel.agent_id == conv.agent_id)
                )
                conv_default_agent_slug = _da.scalar_one_or_none()

            _settings = get_settings()
            _ssl_verify = _settings.llm_ssl_verify

            # Load model configs for this user. Published runs pin the
            # publisher's config (fixed_config_id) — auto routing is disabled
            # for them.
            from api.services.model_credentials import load_model_credentials
            credentials, tier_models, tier_map, pinned_config = await load_model_credentials(
                db, user_id, fixed_config_id=msg.get("fixed_config_id"), ssl_verify=_ssl_verify
            )

            if not tier_models:
                await _send_turn_error(transport, conversation_id, {
                    "type": "error",
                    "message": "No model configured. Go to Settings → AI Models to add a provider and model.",
                })
                return

            # Defaults may fall back, but an explicit selection must never run
            # under a different model than the one shown in the UI.
            config_id_override = msg.get("config_id") or msg.get("provider")
            is_auto = config_id_override == _AUTO_CONFIG_ID
            auto_classification: str | None = None
            target_config: str | None = None
            if pinned_config in ("azure_endpoint_required", "model_config_unavailable"):
                # A published run's pinned model config is unusable — fail
                # with the concrete reason instead of silently running a
                # different model than the publisher bound.
                await _send_turn_error(transport, conversation_id, {
                    "type": "error",
                    "code": pinned_config,
                    "message": (
                        "The selected Azure OpenAI model requires an endpoint. Configure its Base URL in Settings → AI Models."
                        if pinned_config == "azure_endpoint_required"
                        else "The selected model has no usable API key. Configure its API key in Settings → AI Models."
                    ),
                })
                return
            if pinned_config is not None:
                # Pin wins over any client hint: the published run always
                # executes under the publisher's bound model.
                target_config = pinned_config
            elif is_auto:
                # Pre-classify with the cheapest tiered model, then route to
                # the config serving that complexity. Any failure degrades to
                # the default selection — auto is never a hard error.
                if tier_map:
                    cheap_cfg = cheapest_tier(tier_map)
                    if cheap_cfg:
                        from api.services.complexity import classify_complexity
                        try:
                            auto_classification = await classify_complexity(
                                db, conversation_id, msg.get("content", ""),
                                credentials, tier_models, tier_map[cheap_cfg],
                            )
                        except Exception:
                            _log.warning(
                                "Complexity pre-classification failed for conversation=%s, falling back to default model",
                                conversation_id, exc_info=True,
                            )
                    if auto_classification:
                        target_config = resolve_tier_config(tier_map, auto_classification)
                if not target_config:
                    target_config = next((key for key in tier_models if key in credentials), None)
            elif config_id_override:
                if config_id_override in tier_models and config_id_override in credentials:
                    target_config = config_id_override
                else:
                    await _send_turn_error(transport, conversation_id, {
                        "type": "error",
                        "code": "model_config_unavailable",
                        "message": "The selected model has no usable API key. Configure its API key in Settings → AI Models.",
                    })
                    return
            else:
                target_config = next((key for key in tier_models if key in credentials), None)

            if not target_config:
                await _send_turn_error(transport, conversation_id, {
                    "type": "error",
                    "message": "No API key configured. Go to Settings → AI Models and configure an API key first.",
                })
                return
            provider_override = target_config

            # Find project fs_path
            from api.paths import resolve_project_fs_path
            try:
                fs_path = await resolve_project_fs_path(project_id, db)
            except ValueError as e:
                await _send_turn_error(transport, conversation_id, {"type": "error", "message": str(e)})
                return

            # Load agent config — project workspace shadows the global dir.
            from api.paths import AGENTS_DIR
            from api.services.agent_sync import resolve_agent_definition_path
            agent_id = msg.get("agent_id")
            agent_config_path = None
            if agent_id:
                try:
                    agent_config_path = resolve_agent_definition_path(agent_id, fs_path)
                except ValueError:
                    agent_config_path = None
                if not agent_config_path:
                    await _send_turn_error(transport, conversation_id, {
                        "type": "error",
                        "code": "agent_config_not_found",
                        "message": "The selected agent configuration is not available.",
                    })
                    return
            else:
                # Only an omitted agent may use the default definition.
                available = sorted(AGENTS_DIR.glob("*.agent.md"))
                if available:
                    agent_config_path = str(available[0])

            if not agent_config_path:
                await _send_turn_error(transport, conversation_id, {
                    "type": "error",
                    "code": "agent_config_not_found",
                    "message": "No agent configuration is available for the requested agent.",
                })
                return
            if not Path(agent_config_path).exists():
                await _send_turn_error(transport, conversation_id, {
                    "type": "error",
                    "code": "agent_config_not_found",
                    "message": "Agent configuration file not found.",
                })
                return

            agent_config = AgentConfig.from_file(agent_config_path)

            # Skill registry: process-level global + project overlay so project
            # skills shadow global ones with the same slug.
            base_skill_registry: SkillRegistry = ws.app.state.skill_registry
            skill_registry = base_skill_registry.overlay()
            skill_registry.load_dir(Path(fs_path) / "skills")

            # Workflow registry: same project overlay semantics.
            base_workflow_registry = ws.app.state.workflow_registry
            workflow_registry = base_workflow_registry.overlay()
            workflow_registry.load_dir(Path(fs_path) / "workflows")

            # Use process-level knowledge cache
            knowledge_cache = ws.app.state.knowledge_cache

            # Build tool context
            from api.paths import PACKAGE_ROOT
            _s = get_settings()
            tool_context = ToolContext(
                project_id=project_id,
                project_fs_path=fs_path,
                conversation_id=conversation_id,
                user_id=user_id,
                db_session=None,
                knowledge_cache=knowledge_cache,
                framework_root=str(PACKAGE_ROOT),
                secret_key=_s.secret_key,
                session_memories=manager.get_session_memories(conversation_id),
            )
            tool_context.interactive = interactive
            tool_context.ssl_verify = _ssl_verify
            tool_context.browser_ssl_verify = _settings.browser_ssl_verify
            # Per-agent api_request confirmation opt-out (frontmatter flag).
            tool_context.api_request_no_confirm = agent_config.api_request_no_confirm
            # User attachments live in the in-memory upload store (never on
            # disk) for the duration of this turn — expose it to filesystem
            # tools so read_file can serve full content when the inline
            # preview was truncated or omitted.
            from api.routers.media import get_upload, list_upload_names as _list_upload_names
            tool_context.upload_file_lookup = lambda fname: get_upload(conversation_id, fname)
            tool_context.upload_file_names = lambda: _list_upload_names(conversation_id)
            tool_context.integration_settings = {
                "ATLASSIAN_BASE_URL":   _s.atlassian_base_url,
                "ATLASSIAN_AUTH_TYPE":  _s.atlassian_auth_type,
                "JIRA_BASE_PATH":       _s.atlassian_jira_base_path,
                "CONFLUENCE_BASE_PATH": _s.atlassian_confluence_base_path,
                "GIT_BASE_URL":         _s.git_base_url,
                "GIT_API_BASE_PATH":    _s.git_api_base_path,
                "HTTPS_PROXY":          _s.https_proxy,
                "NO_PROXY":             ",".join(filter(None, [_s.no_proxy, _s.app_no_proxy])),
                "SANDBOX_NETWORK":      _s.sandbox_network,
            }

            # Override system integration settings with per-user base_urls
            # stored on user_tokens (Settings -> Integrations).
            from sqlalchemy import text as _sa_text
            _bu_result = await db.execute(
                _sa_text(
                    "SELECT service_key, base_url FROM user_tokens "
                    "WHERE user_id = :uid AND base_url IS NOT NULL"
                ),
                {"uid": user_id},
            )
            _service_to_setting = {
                "git":         "GIT_BASE_URL",
                "jira":        "ATLASSIAN_BASE_URL",
                "confluence":  "CONFLUENCE_BASE_URL",
            }
            for _row in _bu_result:
                _setting_key = _service_to_setting.get(_row[0])
                if _setting_key and _row[1]:
                    tool_context.integration_settings[_setting_key] = _row[1]

            # Load project knowledge (after agent is known — agent frontmatter may filter)
            from api.paths import FRAMEWORK_KNOWLEDGE_DIR
            project_context = await load_project_context(
                project_id=project_id,
                db_session=db,
                cache=knowledge_cache,
                knowledge_filter=agent_config.knowledge or None,
                knowledge_dir=Path(fs_path) / "knowledge",
                framework_knowledge_dir=FRAMEWORK_KNOWLEDGE_DIR,
            )

            # Load conversation history from DB (media_dir enables attachment rehydration).
            # turn_agent_slug lets _load_history mark replies produced by other
            # agents so the @-mentioned agent does not mistake them for its own.
            history = await _load_history(
                db, conversation_id, Path(fs_path) / ".tmp" / "media" / conversation_id,
                turn_agent_slug=agent_config.slug,
            )

            # Load personal memories for this user (scoped to current project)
            personal_memory_ctx = await _load_personal_memories(db, user_id, project_id)

            # Load active task plan for this conversation
            active_plan_ctx = await _load_active_task_plan(db, conversation_id)

            # Scan pending task temp files and append as context
            pending_files_content = await _scan_pending_task_files(
                db, conversation_id, project_id, fs_path
            )

            # Emit context usage
            history_tokens = estimate_history_tokens(history)
            pending_tokens = int(len(pending_files_content.split()) * 1.3) if pending_files_content else 0
            # this used raw ws.send_json — if the client dropped
            # right here (disconnect/reconnect race), send_json raised
            # WebSocketDisconnect inside the turn handler, aborting the turn
            # AFTER the user message was persisted: agent never ran, message
            # lost. Route through the transport like every other push (tolerant
            # of dead connections).
            await _transport_send(transport, conversation_id, {
                "type": "context_usage",
                "static_files": len(project_context.loaded_content),
                "dynamic_files": len(project_context.dynamically_loaded),
                "deferred_files": len(project_context.deferred_entries),
                "overflow_files": len(project_context.overflow_slugs),
                "conversation_history_tokens": history_tokens,
                "pending_task_tokens": pending_tokens,
                "total_budget": agent_config.token_budget,
            })

            content = msg.get("content", "")
            attachments = msg.get("attachments") or []

            # Persist user message immediately so it survives agent crashes.
            # Validation + persistence share the helper used by in-flight
            # injections — same bounds, same row lock, same idempotency.
            persist_result, persist_err = await _prepare_and_persist_user_message(
                db, conversation_id, project_id, fs_path, content, attachments,
                agent_slug=agent_config.slug,
            )
            if persist_err:
                await _send_turn_error(
                    transport, conversation_id, {"type": "error", "message": persist_err}
                )
                return
            attachment_records = persist_result.attachment_records

            # Durable run record for background-turn feedback (a reopened
            # session sees status + partial text). Best-effort: tracking must
            # never fail the turn.
            run_id: str = ""
            try:
                from api.services.conversation_runs import create_run
                run_id = await create_run(conversation_id, str(persist_result.message_id))
            except Exception:
                _log.warning("create_run failed for %s", conversation_id, exc_info=True)

            # Build session
            session = ConversationSession(
                conversation_id=conversation_id,
                project_id=project_id,
                user_id=user_id,
                token_budget=conv.token_budget,
                tokens_used=conv.tokens_used,
            )

            # Register session so conversation_ws can route user_selection_response messages
            manager.register_session(conversation_id, session)
            session_registered = True
            # Drain messages buffered during the claim window (session not yet
            # registered) into the injection queue — the agent consumes them
            # at its first step boundary.
            for pending in manager.drain_pending_injections(conversation_id):
                await _enqueue_injected_message(conversation_id, session, pending)
            # Inject session into tool context so tools can call request_user_selection
            tool_context.session = session

            agent = Agent(
                config=agent_config,
                credentials=credentials,
                tier_models=tier_models,
                skill_registry=skill_registry,
                tool_context=tool_context,
                project_context=project_context,
                db_session=db,
                pending_task_context=pending_files_content,
                personal_memory_context=personal_memory_ctx,
                active_plan_context=active_plan_ctx,
                workflow_registry=workflow_registry,
                # Plan subtasks route by their estimated_complexity only in
                # auto mode; explicit selections keep the session provider.
                # A pinned published config disables auto routing entirely.
                tier_map=tier_map if (is_auto and pinned_config is None) else None,
            )

            # Tools run concurrently with event persistence, so they need their
            # own SQLAlchemy session rather than sharing the handler session.
            tool_db = AsyncSessionLocal()
            tool_context.db_session = tool_db
            # Parallel plan tasks each get their own session via copy_for_task
            # (shared async sessions corrupt under interleaved executes).
            tool_context.db_session_factory = AsyncSessionLocal
            agent._db = tool_db

            # Lazily sync the project's MCP catalog file into mcp_servers so
            # the integration expert's file writes take effect without a
            # restart; attach then reads the table only.  Sync failures must
            # not block the turn — attach degrades to a warning.
            if any(t == "mcp" or t.startswith("mcp:") for t in agent_config.tools):
                try:
                    from api.services.mcp_sync import sync_mcp_servers_from_file
                    await sync_mcp_servers_from_file(
                        db,
                        project_id,
                        Path(fs_path) / "knowledge" / "integrations" / "mcp-servers.md",
                    )
                except Exception as exc:
                    _log.warning("MCP catalog sync failed: %s", exc, exc_info=True)

            # Attach MCP tools if the agent declares mcp / mcp:<slug> markers.
            if any(t == "mcp" or t.startswith("mcp:") for t in agent_config.tools):
                try:
                    from agent_core.tools.mcp_client import attach_mcp_tools
                    mcp_tools = await attach_mcp_tools(tool_context, agent_config.tools)
                    if mcp_tools:
                        agent.add_tools(mcp_tools)
                        _log.info("Attached %d MCP tools for conversation %s", len(mcp_tools), conversation_id)
                except Exception as exc:
                    _log.warning("MCP tool attachment failed: %s", exc, exc_info=True)
                    await session.emit("warning", message=f"MCP servers unavailable: {exc}")

            # Agents that can clone git repos also get the repo_graph tool —
            # the knowledge graph answers structural questions about clones
            # (auto-built on clone/checkout/pull) far cheaper than reading
            # files. Agents declaring repo_graph explicitly keep their own.
            if "git_repo" in agent_config.tools and "repo_graph" not in agent_config.tools:
                try:
                    from agent_core.tools.repo_graph import RepoGraphTool
                    agent.add_tools({"repo_graph": RepoGraphTool()})
                except Exception as exc:
                    _log.warning("repo_graph tool injection failed: %s", exc)

            # An @-mention turn hands this message to a different agent than
            # the conversation default. Say so in the LLM-facing message only -
            # the persisted row keeps the text exactly as the user typed it.
            llm_user_content = content
            if (
                conv_default_agent_slug
                and agent_config.slug != conv_default_agent_slug
                and content.strip()
            ):
                llm_user_content = (
                    f"{content}\n\n"
                    f"[System note: the user mentioned you (@{agent_config.slug}) to handle "
                    f"this message. This conversation is primarily handled by a different "
                    f"agent ({conv_default_agent_slug}); assistant replies in the history "
                    f"prefixed with [name] were produced by other agents, not by you.]"
                )

            async def run_agent():
                try:
                    await agent.run(
                        user_message=llm_user_content,
                        history=history,
                        session=session,
                        provider_override=provider_override,
                        attachments=attachment_records,
                        auto_tier=auto_classification,
                    )
                finally:
                    await session.close()

            event_db = AsyncSessionLocal()
            # Set by the stream_end persist below; the handler tail waits on it
            # after a drain timeout so event_db isn't closed under the persist.
            _persist_guard: asyncio.Task | None = None
            # Set by the user_message_injected persist below — same detached-
            # task hazard: it must finish (or be cancelled) before the drain
            # loop runs on the same event_db session.
            _inj_guard: asyncio.Task | None = None
            # task_ids that received a terminal event (completed/failed/skipped)
            # this turn — the stale-task reconcile must never flip those rows.
            _terminal_task_ids: set[str] = set()
            # task_ids deferred by an in-flight injection (task mode) — the
            # reconcile must leave them pending for the agent's continuation.
            _deferred_task_ids: set[str] = set()

            async def forward_events():
                nonlocal _persist_guard, _inj_guard, _terminal_task_ids, _deferred_task_ids
                _text_buf: str = ""
                _last_snap_ts = time.monotonic()
                # Model that actually executed this turn (model_selected event
                # carries the resolved model for auto routing, the chosen
                # config's model otherwise). Stored on the assistant row at
                # stream_end so reloads keep the attribution.
                _model_name: str | None = None
                _tool_calls_buf: list[dict] = []
                _images_buf: list[dict] = []
                _files_buf: list[dict] = []
                # Turn-level LLM failure (provider exception / request over
                # limit). Persisted on the run row at stream_end — the live
                # error bubble is client-only and would vanish on reload.
                _turn_error: str | None = None

                async for event in session.events():
                    # Forward to the client via the transport.  If no WS is
                    # connected (user switched away), the event is silently
                    # dropped - but DB persistence below still runs.
                    await _transport_send(
                        transport,
                        conversation_id,
                        {"type": event.type, **event.data},
                    )

                    # Persist events to DB alongside forwarding
                    try:
                        if event.type == "model_selected":
                            _model_name = event.data.get("model") or None
                        elif event.type == "stream_delta":
                            _text_buf += event.data.get("delta", "")
                            session.current_streaming_text = _text_buf
                            # Throttled snapshot for interruption recovery
                            # (the Message row stays authoritative for the
                            # final text of completed turns).
                            if run_id and time.monotonic() - _last_snap_ts >= 5.0:
                                _last_snap_ts = time.monotonic()
                                try:
                                    from api.services.conversation_runs import update_run_snapshot
                                    await update_run_snapshot(run_id, _text_buf)
                                except Exception:
                                    _log.debug("update_run_snapshot failed for %s", conversation_id, exc_info=True)
                        elif event.type == "tool_call_start":
                            _tool_calls_buf.append({
                                "call_id": event.data.get("call_id"),
                                "tool": event.data.get("tool"),
                                "input": event.data.get("input", {}),
                                "status": "running",
                                "task_id": event.data.get("task_id"),
                                "current_step": event.data.get("current_step"),
                                "next_step": event.data.get("next_step"),
                            })
                            session.current_tool_calls = list(_tool_calls_buf)
                        elif event.type == "tool_call_end":
                            call_id = event.data.get("call_id")
                            output = event.data.get("output", {})
                            # Truthy check on the error value, matching the
                            # frontend (output.error): an output with a null/
                            # empty error key is not a failure.
                            tc_status = event.data.get("status") or ("error" if isinstance(output, dict) and output.get("error") else "done")
                            matched = False
                            for tc in _tool_calls_buf:
                                if tc["call_id"] == call_id:
                                    tc["output"] = output
                                    tc["status"] = tc_status
                                    matched = True
                            if not matched:
                                _log.warning(
                                    "Received tool_call_end without matching start: conversation=%s call_id=%s tool=%s buffered_calls=%d",
                                    conversation_id,
                                    call_id,
                                    event.data.get("tool"),
                                    len(_tool_calls_buf),
                                )
                            session.current_tool_calls = list(_tool_calls_buf)
                        elif event.type == "image_output":
                            _images_buf.extend(event.data.get("images", []))
                        elif event.type == "file_output":
                            # A turn often delivers the same deliverable
                            # several times (generate -> verify -> fix all
                            # rewrite the same OUTPUT_DIR file, and every
                            # write auto-delivers). Same name = same
                            # deliverable: replace in place so the chat
                            # shows one download link per file.
                            _upsert_file_records(_files_buf, event.data.get("files", []))
                        elif event.type == "error":
                            err_msg = event.data.get("message", "error")
                            _turn_error = err_msg
                            for tc in _tool_calls_buf:
                                if tc["status"] == "running":
                                    tc["output"] = {"error": err_msg}
                                    tc["status"] = "error"
                        elif event.type == "stream_end":
                            msg_id = event.data.get("message_id")
                            # Terminal run-state transition. The snapshot is
                            # kept for interrupted runs (recovery); completed
                            # runs leave the Message row authoritative. Turns
                            # that ended in a failure (provider exception,
                            # refusal, empty output) must NOT be marked
                            # "completed" — the frontend's RunNotice + rerun
                            # affordance only fires on failed/interrupted, and
                            # a completed run with no message row looks like a
                            # silently vanished turn after reload.
                            _stop_reason = event.data.get("stop_reason")
                            _has_content = bool(_text_buf or _tool_calls_buf or _images_buf or _files_buf)
                            if _stop_reason in ("aborted", "interrupted"):
                                _run_status = "interrupted"
                            elif _stop_reason == "api_error" or _turn_error:
                                _run_status = "failed"
                            elif _stop_reason == "refusal":
                                _run_status = "failed"
                                _turn_error = _turn_error or "Model refused to respond"
                            elif _stop_reason == "context_exceeded":
                                _run_status = "failed"
                                _turn_error = _turn_error or "Conversation exceeds the model's context window"
                            elif _stop_reason == "content_filter":
                                _run_status = "failed"
                                _turn_error = _turn_error or "Response blocked by the content filter"
                            elif _stop_reason in ("max_tokens", "pause_turn_exhausted") and not _has_content:
                                # Truncated before any text arrived — nothing
                                # to persist, but "no output" would mislead.
                                _run_status = "failed"
                                _turn_error = _turn_error or "Response truncated before any output"
                            elif not _has_content:
                                # Nothing to persist and no error: an empty
                                # model response. Mark it failed so the reopen
                                # shows a notice + rerun instead of a question
                                # that silently went unanswered.
                                _run_status = "failed"
                                _turn_error = _turn_error or "Model returned no output"
                            else:
                                _run_status = "completed"
                            # Whether the interrupted partial output landed in a
                            # Message row below (drives the snapshot decision at
                            # finish_run: a persisted partial needs no recovery
                            # snapshot - see materialize_interrupted_snapshots).
                            _persisted_interrupted = (
                                _run_status == "interrupted"
                                and bool(msg_id and (_has_content or _turn_error))
                            )
                            if (msg_id and (_has_content or _turn_error)):
                                _log.info(
                                    "Persisting assistant stream: conversation=%s message_id=%s text_chars=%d tool_calls=%d plan_task_calls=%d images=%d files=%d error=%s",
                                    conversation_id,
                                    msg_id,
                                    len(_text_buf),
                                    len(_tool_calls_buf),
                                    sum(tc.get("tool") == "plan_task" for tc in _tool_calls_buf),
                                    len(_images_buf),
                                    len(_files_buf),
                                    bool(_turn_error),
                                )
                                # Shield the persist: a teardown cancel (drain
                                # timeout, handler cancellation) landing mid-commit
                                # would silently roll back the whole assistant
                                # message. The shielded task runs to completion.
                                _persist_guard = asyncio.create_task(
                                    _persist_assistant_message(
                                        event_db, conversation_id,
                                        _text_buf if _has_content else (_turn_error or ""),
                                        _tool_calls_buf, msg_id,
                                        images=_images_buf if _images_buf else None,
                                        files=_files_buf if _files_buf else None,
                                        interrupted=_run_status == "interrupted",
                                        error=_run_status == "failed",
                                        agent_slug=agent_config.slug,
                                        model_name=_model_name,
                                    )
                                )
                                try:
                                    await asyncio.shield(_persist_guard)
                                except asyncio.CancelledError:
                                    # forward_task is being torn down; let the
                                    # persist finish before unwinding.
                                    await _persist_guard
                                    raise
                            if run_id:
                                try:
                                    from api.services.conversation_runs import finish_run
                                    await finish_run(
                                        run_id, _run_status,
                                        error_message=_turn_error if _run_status == "failed" else None,
                                        tokens_used=int(event.data.get("total_tokens") or 0),
                                        # Keep the snapshot ONLY when the partial
                                        # output never made it into a Message row:
                                        # the startup sweep materializes it later.
                                        # A persisted turn already carries its
                                        # partial in history - a kept snapshot
                                        # would duplicate it on recovery.
                                        snapshot=(
                                            _text_buf
                                            if _run_status == "interrupted"
                                            and not _persisted_interrupted
                                            else None
                                        ),
                                    )
                                except Exception:
                                    _log.warning("finish_run(stream_end) failed for %s", conversation_id, exc_info=True)
                            _turn_error = None
                            _text_buf = ""
                            _tool_calls_buf = []
                            _images_buf = []
                            _files_buf = []
                            session.current_streaming_text = ""
                            session.current_tool_calls = []
                        elif event.type == "user_message_injected":
                            # Persist the injected message BEFORE resolving the
                            # ack — the agent only continues once the message is
                            # durably stored. Shielded so a teardown cancel
                            # cannot skip the persist (the message is already
                            # part of the LLM history at that point).
                            _inj_mid = event.data.get("message_id")
                            _inj_content = event.data.get("content", "")
                            # Named task (like _persist_guard): a teardown
                            # cancel detaches the shield's inner task, which
                            # must be tracked so the tail can wait on it
                            # before draining on the same event_db session.
                            _inj_guard = asyncio.create_task(
                                _prepare_and_persist_user_message(
                                    event_db, conversation_id, project_id, fs_path,
                                    _inj_content, event.data.get("attachments") or [],
                                    set_title=False, message_id=_inj_mid,
                                    agent_slug=agent_config.slug,
                                )
                            )
                            _inj_result, _inj_err = await asyncio.shield(_inj_guard)
                            if _inj_err is not None:
                                session.resolve_input_persisted(_inj_mid, False)
                                await _transport_send(transport, conversation_id, {
                                    "type": "input_rejected",
                                    "message_id": _inj_mid,
                                    "content": _inj_content,
                                    "message": _inj_err,
                                })
                            else:
                                session.resolve_input_persisted(
                                    _inj_mid, True, _inj_result.attachment_records
                                )
                                await _transport_send(transport, conversation_id, {
                                    "type": "user_message_injected",
                                    "message_id": _inj_mid,
                                    "content": _inj_content,
                                    "sequence_num": _inj_result.sequence_num,
                                })
                        elif event.type == "agentic_loop_completed":
                            # Normalize like the persist path: rows store
                            # _norm_task_id (capped to 36 chars), so an
                            # uncapped raw id here would fail the sweep's
                            # notin_ match and get the deferred task swept
                            # to failed while the agent still works on it.
                            _deferred_task_ids.update(
                                _norm_task_id(t) for t in (event.data.get("deferred_task_ids") or [])
                            )
                        elif event.type == "task_plan_created":
                            await _persist_tasks(
                                event_db, conversation_id, event.data.get("tasks", [])
                            )
                            await _persist_task_event(
                                event_db, conversation_id, event.type, None, event.data
                            )
                        elif event.type == "task_started":
                            await _update_task_status(
                                event_db, conversation_id, event.data.get("task_id", ""), "running",
                                current_step=event.data.get("current_step"),
                                next_step=event.data.get("next_step"),
                                progress_completed=event.data.get("progress_completed"),
                                progress_total=event.data.get("progress_total"),
                                actual_model=event.data.get("actual_model"),
                            )
                            await _persist_task_event(
                                event_db, conversation_id, event.type,
                                event.data.get("task_id"), event.data,
                            )
                        elif event.type == "task_progress":
                            await _update_task_progress(
                                event_db, conversation_id, event.data.get("task_id", ""), event.data
                            )
                            await _persist_task_event(
                                event_db, conversation_id, event.type,
                                event.data.get("task_id"), event.data,
                            )
                        elif event.type in ("task_completed", "task_failed", "task_skipped"):
                            await _persist_terminal_task_event(event_db, conversation_id, event)
                            if event.data.get("task_id"):
                                # _norm_task_id keeps this set in sync with the
                                # capped ids persisted in agent_tasks, or the
                                # stale-task sweep would flip the completed
                                # row to failed (notin_ mismatch).
                                _terminal_task_ids.add(_norm_task_id(event.data.get("task_id")))
                    except asyncio.CancelledError:
                        # CancelledError is a BaseException — it must be surfaced
                        # (the previous silent catchless unwind dropped the
                        # assistant message with no log at all).
                        logging.getLogger("agents_universe.ws").warning(
                            "Persist of event %s cancelled for conversation=%s — DB task status may be stale",
                            event.type, conversation_id,
                        )
                        raise
                    except Exception:
                        await event_db.rollback()
                        logging.getLogger("agents_universe.ws").warning(
                            "Failed to persist event %s to DB: conversation=%s message_id=%s call_id=%s tool=%s buffered_calls=%d",
                            event.type,
                            conversation_id,
                            event.data.get("message_id"),
                            event.data.get("call_id"),
                            event.data.get("tool"),
                            len(_tool_calls_buf),
                            exc_info=True,
                        )

            agent_task = asyncio.create_task(run_agent())
            forward_task = asyncio.create_task(forward_events())

            # Wire abort: set flag AND cancel agent_task so the current await
            # (LLM stream or tool HTTP call) is interrupted immediately.
            # Must be created after agent_task so the closure captures it.
            abort_task: asyncio.Task | None = None
            abort_event = manager.get_abort_event(conversation_id)
            if abort_event:
                async def _watch_abort(_at: asyncio.Task = agent_task) -> None:
                    try:
                        await abort_event.wait()
                        session.abort()
                        _at.cancel()
                    except Exception:
                        _log.debug("Abort watcher exception for %s", conversation_id, exc_info=True)
                abort_task = asyncio.create_task(_watch_abort())

            # Wait for agent to finish; CancelledError means user hit Stop.
            _aborted = False
            try:
                await agent_task
            except asyncio.CancelledError:
                _aborted = True
            except Exception as agent_exc:
                _log.error(
                    "Agent task failed: conversation=%s correlation_id=%s error_type=%s error=%s",
                    conversation_id,
                    correlation_ctx_var.get(),
                    type(agent_exc).__name__,
                    agent_exc,
                    exc_info=True,
                )
                error_message = "Agent execution failed. Check server logs for details."
                err_msg_id = str(_uuid_mod.uuid4())
                await _transport_send(transport, conversation_id, {"type": "error", "message": error_message, "stream_message_id": err_msg_id})
                await _transport_send(transport, conversation_id, {"type": "stream_end", "message_id": err_msg_id, "total_tokens": 0})
                if run_id:
                    try:
                        from api.services.conversation_runs import finish_run
                        await finish_run(run_id, "failed", error_message=error_message, tokens_used=0)
                    except Exception:
                        _log.warning("finish_run(agent error) failed for %s", conversation_id, exc_info=True)

            # Agent is done and session.close() was called — forward_task will
            # see the None sentinel and exit naturally. Give it a short timeout.
            try:
                await asyncio.wait_for(forward_task, timeout=30.0)
            except asyncio.TimeoutError:
                forward_task.cancel()
                # Let the cancelled task unwind completely — its except/finally
                # (rollback/close on event_db) must finish BEFORE the drain
                # loop below touches the same AsyncSession; two coroutines
                # sharing one session interleave commits and rollbacks on the
                # same greenlet. Short timeout so a stuck finally can't hang
                # the cleanup forever.
                try:
                    await asyncio.wait_for(forward_task, timeout=5.0)
                except (asyncio.CancelledError, Exception):
                    pass
                if _persist_guard is None:
                    # forward_task was cancelled before it consumed stream_end
                    # (or the stream carried no persistable content). If the
                    # turn had streamed text, the assistant message is lost
                    # from history — surface it loudly instead of silently.
                    _log.error(
                        "forward_task timed out before stream_end persist started for %s — assistant message may be lost",
                        conversation_id,
                    )
                # The stream_end persist is shielded, so cancelling forward_task
                # can't roll it back — but wait for it before closing event_db.
                elif not _persist_guard.done():
                    try:
                        await asyncio.wait_for(_persist_guard, timeout=60.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        _persist_guard.cancel()
                        _log.error(
                            "Assistant message persist did not complete for %s — message may be lost",
                            conversation_id,
                        )
                # The injection persist is shielded inside forward_events — it
                # survives the task's cancellation as a detached task still
                # owning event_db. Wait it out before the drain loop touches
                # the same session (interleaved commits/rollbacks would
                # corrupt both).
                if _inj_guard is not None and not _inj_guard.done():
                    try:
                        await asyncio.wait_for(_inj_guard, timeout=60.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        _inj_guard.cancel()
                        _log.warning(
                            "Injected message persist did not complete for %s — message may be lost",
                            conversation_id,
                        )
                # The drain timed out: terminal task events still sitting in the
                # queue would be swept to failed below — drain and persist them
                # directly (bounded: queue ≤ maxsize, agent already finished).
                while True:
                    try:
                        _ev = session._event_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if _ev is None:
                        break
                    if _ev.type in ("task_completed", "task_failed", "task_skipped"):
                        try:
                            await _persist_terminal_task_event(event_db, conversation_id, _ev)
                        except Exception:
                            _log.warning(
                                "Failed to persist drained terminal event %s for %s",
                                _ev.type, conversation_id, exc_info=True,
                            )
            except Exception:
                _log.debug("forward_task cleanup exception for %s", conversation_id, exc_info=True)

            # Abort or crash leaves task_plan rows stuck at pending/running:
            # agent-core's abort paths never emit a terminal event for the
            # remaining tasks, and CancelledError often bypasses them entirely
            # — those rows would block project deletion forever
            # (PROJECT_HAS_RUNNING_WORK). The turn has truly ended here and
            # forward_events has drained every event, so reconcile any rows
            # that STILL have no terminal status. Idempotent: terminal rows and
            # the next turn's fresh rows are never touched. Aborted turns
            # become skipped (the work never ran — grey, not red); only
            # genuinely failed turns mark leftovers failed.
            try:
                from sqlalchemy import update as _sa_update
                from api.models.conversation import AgentTask as _AgentTaskM

                _sweep = _sa_update(_AgentTaskM).where(
                    _AgentTaskM.conversation_id == conversation_id,
                    _AgentTaskM.status.in_(("pending", "running")),
                )
                if _terminal_task_ids:
                    _sweep = _sweep.where(_AgentTaskM.task_id.notin_(_terminal_task_ids))
                if _deferred_task_ids:
                    # Deferred by an in-flight injection — kept pending for the
                    # agent's continuation instead of being swept to failed.
                    _sweep = _sweep.where(_AgentTaskM.task_id.notin_(_deferred_task_ids))
                _abort = session.is_aborted()
                _stale = await event_db.execute(
                    _sweep.values(
                        status=("skipped" if _abort else "failed"),
                        error_message=("Turn aborted" if _abort else "Agent execution ended without task completion"),
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                if _stale.rowcount:
                    await event_db.commit()
            except Exception:
                _log.debug("Finalize stale tasks failed for %s", conversation_id, exc_info=True)

            await event_db.close()
            await tool_db.close()

            # After forward drains, tell the client the abort completed.
            if _aborted:
                if run_id:
                    try:
                        from api.services.conversation_runs import finish_run
                        await finish_run(run_id, "interrupted", tokens_used=session.tokens_used)
                    except Exception:
                        _log.warning("finish_run(abort) failed for %s", conversation_id, exc_info=True)
                await _transport_send(transport, conversation_id, {"type": "abort_ack"})
                await _transport_send(transport, conversation_id, {"type": "stream_end", "message_id": None, "total_tokens": session.tokens_used})

            manager.deregister_session(conversation_id)

            from sqlalchemy import update
            tokens_delta = session.tokens_used - conv.tokens_used
            await db.execute(
                update(Conversation)
                .where(Conversation.conversation_id == conversation_id)
                .values(tokens_used=Conversation.tokens_used + tokens_delta)
            )
            await db.commit()

    except BaseException as e:
        _log.error("agent turn outer exception for %s (%s)", conversation_id, type(e).__name__, exc_info=True)
        error_message = "Agent execution failed. Check server logs for details."
        err_msg_id = str(_uuid_mod.uuid4())
        # Error path: DB sessions opened mid-turn (tool_db / event_db) were not
        # closed by the normal drain path — close them here to avoid leaking
        # connection pool slots. close() is idempotent, so double-close is safe.
        # BaseException (incl. CancelledError) is caught so a cancelled turn
        # still releases its sessions instead of leaking pool slots.
        for _sess_name in ("event_db", "tool_db"):
            _sess = locals().get(_sess_name)
            if _sess is not None:
                try:
                    await _sess.close()
                except Exception:
                    _log.debug("Failed to close %s for %s", _sess_name, conversation_id, exc_info=True)
        try:
            await _transport_send(transport, conversation_id, {"type": "error", "message": error_message, "stream_message_id": err_msg_id})
            await _transport_send(transport, conversation_id, {"type": "stream_end", "message_id": err_msg_id, "total_tokens": 0})
        except Exception:
            _log.debug("Failed to send outer error to client for %s", conversation_id)
        # Mark the run failed before re-raising — the finally-tail safety
        # net would otherwise flip it to interrupted, losing the attribution.
        _run_id = locals().get("run_id")
        if _run_id:
            try:
                from api.services.conversation_runs import finish_run
                await finish_run(_run_id, "failed", error_message="Agent execution failed. Check server logs for details.")
            except Exception:
                _log.debug("finish_run(outer error) failed for %s", conversation_id, exc_info=True)
        # Re-raise so task cancellation stays visible (the task wrapper logs
        # non-cancellation exceptions) — the finally block below still runs.
        raise
    finally:
        # Safety net: a turn that exited without a terminal write (e.g. a
        # cancellation that never reached stream_end) leaves the run row
        # 'running' — flip it so reopen shows an interrupted notice, not a
        # zombie. finish_run's status guard makes this a no-op after a
        # normal terminal write.
        _run_id = locals().get("run_id")
        if _run_id:
            try:
                from api.services.conversation_runs import finish_run
                await finish_run(_run_id, "interrupted")
            except Exception:
                _log.debug("finish_run(finally) failed for %s", conversation_id, exc_info=True)
        # Release per-turn resources even on error paths: provider HTTP
        # clients, in-memory uploads, and correlation context.
        # Deregister the session on every exit path (normal end already
        # deregistered; without this, an exception mid-turn leaves the
        # session registered forever and is_session_active() blocks every
        # future message on the conversation).
        manager.deregister_session(conversation_id)
        # Clean up the abort watcher on EVERY exit path (normal, error,
        # cancellation) — otherwise each failed/cancelled turn leaks a
        # watcher holding the closed session, the finished agent_task and
        # the abort event until the next Stop on the same conversation.
        _abort_watcher = locals().get("abort_task")
        if _abort_watcher is not None and not _abort_watcher.done():
            try:
                _abort_watcher.cancel()
            except Exception:
                _log.debug("Failed to cancel abort_task for %s", conversation_id)
        # Buffered injections must NOT be discarded here: a message sent in
        # the window after deregister_session (tokens update, commit,
        # agent.close) was buffered with an input_queued ack and no watchdog
        # — dropping it would lose the user's message for good. The next
        # turn's drain (after register_session) consumes whatever is buffered.
        # EXCEPTION: a turn that died BEFORE register_session (conversation
        # lookup, provider config, or any early exception) never drained
        # anything — its buffer belongs to a dead turn, and the next turn's
        # drain would inject stale messages into an unrelated turn. Discard
        # them; the UI's input_queued settles on the failed turn.
        if not session_registered:
            manager.discard_pending_injections(conversation_id)
        # In-memory uploads and correlation context first: a reconnecting
        # client can grab the turn claim the moment release_turn() runs, and
        # drop_uploads() would then wipe the uploads the new turn is
        # validating mid-flight ("Attachment expired"). Drop only uploads from
        # BEFORE this turn — a file attached while the agent was running
        # belongs to the user's next send, not to this turn's cleanup.
        from api.routers.media import drop_uploads
        drop_uploads(conversation_id, older_than=turn_started)
        request_id_var.reset(req_token)
        correlation_ctx_var.reset(ctx_token)
        manager.release_turn(conversation_id)
        # provider.close() is the only remaining await: keep it last so a
        # cancellation landing here cannot skip the cleanup above.
        _agent = locals().get("agent")
        if _agent is not None:
            try:
                await _agent.close()
            except Exception:
                _log.debug("Failed to close agent for %s", conversation_id, exc_info=True)


async def _transport_send(transport: Transport | None, conversation_id: str, data: dict) -> bool:
    """Forward an event to the transport (WS manager / SSE stream).

    None transport = pure WS path, resolved to the manager adapter at the
    top of run_turn. Kept as a function so nested closures reference it by
    name without rebinding.
    """
    if transport is None:
        return False
    try:
        return await transport.send(conversation_id, data)
    except Exception:
        _log.debug("transport.send failed for %s", conversation_id, exc_info=True)
        return False


# ── History loading ──────────────────────────────────────────────────────


async def _load_history(
    db, conversation_id: str, media_dir: Path, max_messages: int = 200,
    turn_agent_slug: str | None = None,
) -> list:
    """Load conversation messages from DB as provider Message objects.

    Loads the most recent max_messages to avoid unbounded memory usage on
    long conversations. The agent's history compressor handles summarization.
    Reconstructs tool_calls and tool_call_id for correct LLM round-trips.
    Rehydrates user attachment references into multimodal content parts.

    When *turn_agent_slug* is set, assistant messages produced by a different
    agent (@-mention turns) are prefixed "[display_name]:" so the current
    agent can tell its own replies from other agents'.
    """
    import json as _json
    from pathlib import Path
    from sqlalchemy import select
    from agent_core.providers.base import Message
    from api.models.conversation import Message as DbMessage

    # Slug -> display_name for the annotation prefix (agents table is small;
    # a full scan beats a per-message subquery on a 200-message history).
    agent_display_names: dict[str, str] = {}
    if turn_agent_slug:
        from api.models.agent import Agent as AgentModel
        _names = await db.execute(select(AgentModel.slug, AgentModel.display_name))
        agent_display_names = {slug: name for slug, name in _names.all()}

    # Single query: fetch last N messages ordered descending, then reverse
    query = (
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num.desc())
        .limit(max_messages)
    )
    result = await db.execute(query)
    raw_messages = list(reversed(result.scalars().all()))

    # Drop oldest messages if cumulative content exceeds 50 MB to prevent OOM.
    # accumulation ran oldest→newest and broke on the first message
    # over budget — keeping the OLDEST messages and silently discarding the
    # most recent ones (the semantic core of the conversation). Accumulate
    # newest-first and keep the latest contiguous segment instead.
    MAX_HISTORY_BYTES = 50 * 1024 * 1024
    total_bytes = 0
    db_messages = []
    for m in reversed(raw_messages):  # newest → oldest
        msg_bytes = len((m.content or "").encode("utf-8"))
        if total_bytes + msg_bytes > MAX_HISTORY_BYTES:
            break
        total_bytes += msg_bytes
        db_messages.append(m)
    db_messages.reverse()  # restore chronological order for history building

    dropped_count = len(raw_messages) - len(db_messages)

    history = []
    if dropped_count > 0:
        # Inject a synthetic exchange so the agent knows context was trimmed
        history.append(Message(
            role="user",
            content=f"[{dropped_count} earlier messages were removed from context due to size limits. The conversation below is a partial history.]",
        ))
        history.append(Message(role="assistant", content="Understood, I'll continue from the available context."))
    # Count attachment turns up front so the vision budget below lands on the
    # NEWEST _REHYDRATE_IMAGE_TURNS turns. Counting inside the loop would
    # hydrate the OLDEST ones instead — the current task's screenshots (the
    # most relevant) are the ones that need vision.
    total_image_turns = 0
    for m in db_messages:
        if m.role != "user" or not m.knowledge_refs:
            continue
        try:
            refs = _json.loads(m.knowledge_refs)
        except (ValueError, TypeError):
            continue
        if refs.get("images"):
            total_image_turns += 1
    rehydrated_image_turns = 0
    for m in db_messages:
        tool_calls = None
        tool_outputs: list[tuple[str, str, str]] = []  # (call_id, tool_name, output_json)
        if m.tool_calls:
            try:
                raw_tcs = _json.loads(m.tool_calls)
                tool_calls = []
                for tc in raw_tcs:
                    if "type" in tc and "function" in tc:
                        call_id = tc.get("id", "")
                        tool_calls.append(tc)
                        tool_outputs.append((call_id, tc["function"].get("name", ""), "{}"))
                    else:
                        call_id = tc.get("call_id") or tc.get("id") or ""
                        tool_name = tc.get("tool") or tc.get("name") or ""
                        output = tc.get("output", {})
                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": _json.dumps(tc.get("input", {})) if isinstance(tc.get("input"), dict) else (tc.get("arguments") or "{}"),
                            },
                        })
                        tool_outputs.append((call_id, tool_name, _json.dumps(output) if isinstance(output, dict) else str(output or "{}")))
                tool_calls = tool_calls or None
            except (ValueError, TypeError):
                pass

        # Tool result messages need tool_call_id and name
        tool_call_id = None
        name = None
        if m.role == "tool" and m.knowledge_refs:
            try:
                refs = _json.loads(m.knowledge_refs)
                tool_call_id = refs.get("tool_call_id")
                name = refs.get("tool_name")
            except (ValueError, TypeError):
                pass

        # Rehydrate user attachment references into multimodal content parts.
        # Images become vision blocks only for the most recent
        # _REHYDRATE_IMAGE_TURNS attachment turns (protects the context
        # budget); older attachments and missing files degrade to text refs.
        content: str | list = m.content or ""
        refs = None
        if m.knowledge_refs:
            try:
                refs = _json.loads(m.knowledge_refs)
            except (ValueError, TypeError):
                refs = None
        if m.role == "user" and isinstance(refs, dict) and (refs.get("attachments") or refs.get("images")):
            atts = refs.get("attachments") or []
            imgs = refs.get("images") or []
            parts: list[dict] = []
            if m.content:
                parts.append({"type": "text", "text": m.content})
            for a in atts:
                if not isinstance(a, dict) or not a.get("url"):
                    continue
                fp = media_dir / Path(str(a["url"])).name
                if fp.is_file():
                    prepared = await _prepare_attachment(a, fp, conversation_id)
                    if prepared.get("inline_text"):
                        # Old rows may lack a name key — fall back to the URL
                        # so a missing field can't crash the whole turn.
                        parts.append({"type": "text", "text": f"### Attachment: {prepared.get('name', '') or a.get('name', '')}\n{prepared['inline_text']}"})
                        continue
                parts.append({"type": "text", "text": _attachment_ref_text(a, conversation_id)})
            # total <= _REHYDRATE_IMAGE_TURNS: every turn hydrates (0 >= 0).
            # total > limit: only the newest limit turns get vision.
            hydrate_imgs = rehydrated_image_turns >= max(0, total_image_turns - _REHYDRATE_IMAGE_TURNS)
            for img in imgs:
                if not isinstance(img, dict) or not img.get("url"):
                    continue
                fp = media_dir / Path(str(img["url"])).name
                if hydrate_imgs and fp.is_file():
                    prepared = await _prepare_attachment(img, fp, conversation_id)
                    if prepared.get("image_data"):
                        parts.append({"type": "image", "media_type": prepared["image_media_type"], "data": prepared["image_data"]})
                        continue
                parts.append({"type": "text", "text": f"Attachment image: {img.get('alt', img.get('url', ''))} — file path: .tmp/media/{conversation_id}/{fp.name}"})
            if imgs:
                rehydrated_image_turns += 1
            content = parts

        # Attribute another agent's reply so the current agent does not read
        # it as its own prior output. Text-bearing turns only - an empty
        # tool-only turn has nothing to attribute.
        if (
            turn_agent_slug
            and m.role == "assistant"
            and m.agent_slug
            and m.agent_slug != turn_agent_slug
            and isinstance(content, str)
            and content.strip()
        ):
            display = agent_display_names.get(m.agent_slug, m.agent_slug)
            content = f"[{display}]:\n{content}"

        history.append(Message(
            role=m.role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
        ))

        # Synthesize tool response messages for each tool_call.
        # OpenAI requires a tool message for every tool_call_id in an assistant message.
        # Tool results are stored inline in the assistant's tool_calls JSON, not as separate DB rows.
        if tool_calls and m.role == "assistant":
            for tc_id, tc_name, tc_output in tool_outputs:
                if not tc_id:
                    continue
                history.append(Message(
                    role="tool",
                    content=tc_output,
                    tool_call_id=tc_id,
                    name=tc_name,
                ))

    return history


async def _load_personal_memories(db, user_id: str, project_id: str | None = None) -> str:
    """Load personal memories for this user (scoped by project) and return as context text."""
    try:
        import json as _json
        from sqlalchemy import select
        from api.models.memory import PersonalMemory
        query = select(PersonalMemory).where(
            PersonalMemory.user_id == user_id,
            PersonalMemory.is_archived == False,  # noqa: E712
        )
        if project_id:
            query = query.where(
                (PersonalMemory.project_id == project_id) | (PersonalMemory.project_id == None)  # noqa: E711
            )
        query = query.order_by(PersonalMemory.created_at.desc()).limit(50)
        result = await db.execute(query)
        memories = result.scalars().all()
        if not memories:
            return ""
        lines = []
        for m in memories:
            if not m.content:
                continue
            scope_label = "project" if m.project_id else "global"
            tag_list: list[str] = []
            try:
                tag_list = _json.loads(m.tags) if m.tags else []
            except (ValueError, TypeError):
                pass
            tag_str = "][".join(tag_list[:3]) if tag_list else ""
            prefix = f"[{scope_label}]"
            if tag_str:
                prefix += f"[{tag_str}]"
            lines.append(f"- {prefix} {m.content}")
        return "## Personal Memories\n\n" + "\n".join(lines) if lines else ""
    except Exception:
        _log.warning("Failed to load personal memories for user %s", user_id, exc_info=True)
        return ""


async def _load_active_task_plan(db, conversation_id: str) -> str:
    """Return a formatted task plan if there are any tasks for this conversation."""
    from sqlalchemy import select
    from api.models.conversation import AgentTask

    result = await db.execute(
        select(AgentTask)
        .where(AgentTask.conversation_id == conversation_id)
        .order_by(AgentTask.sequence_num)
    )
    tasks = result.scalars().all()
    if not tasks:
        return ""

    STATUS_ICON = {
        "completed": "✓",
        "running": "→",
        "failed": "✗",
        "skipped": "-",
    }
    lines = ["## Current Task Plan"]
    for task in tasks:
        icon = STATUS_ICON.get(task.status, " ")
        suffix = f" ({task.status})" if task.status not in ("pending", "completed") else ""
        # Outcome detail per task: a resumed conversation (previous run
        # interrupted) must let the agent tell finished work from work to
        # redo without re-reading the whole history. Truncated - the summary
        # is a pointer, not a transcript.
        detail = ""
        if task.status == "completed" and task.result_summary:
            detail = f" - {task.result_summary[:200]}"
        elif task.status == "failed" and task.error_message:
            detail = f" - {task.error_message[:200]}"
        lines.append(f"[{icon}] {task.sequence_num}. {task.title}{suffix}{detail}")
    return "\n".join(lines)


def _read_task_files_sync(conv_media_path: str) -> list[str]:
    """Read non-image task output files from media directory. Runs in a thread."""
    from pathlib import Path
    conv_media = Path(conv_media_path)
    if not conv_media.exists():
        return []
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    parts = []
    for f in sorted(conv_media.iterdir()):
        if f.is_file() and f.suffix.lower() not in image_exts:
            try:
                content = f.read_text(encoding="utf-8")
                if len(content) < 5000:
                    parts.append(f"### {f.name}\n{content}")
            except (UnicodeDecodeError, OSError):
                pass
    return parts


@dataclass
class _PersistUserMessageResult:
    """Outcome of persisting a user message: the DB sequence number plus the
    prepared attachment records the agent needs to build LLM user content."""

    sequence_num: int
    message_id: str
    image_records: list[dict]
    user_attachments: list[dict]
    attachment_records: list[dict]


async def _prepare_and_persist_user_message(
    db,
    conversation_id: str,
    project_id: str,
    fs_path: str,
    content: str,
    attachments: list,
    *,
    set_title: bool = True,
    message_id: str | None = None,
    agent_slug: str | None = None,
) -> tuple[_PersistUserMessageResult | None, str | None]:
    """Validate + persist a user message, returning (result, None) on success
    or (None, error_message) on validation failure.

    Shared by the normal turn path, the in-flight injection path and the
    watchdog fallback, so every path enforces the same bounds. The caller owns
    the DB session (must be writable); the conversation row is locked to
    serialize sequence number assignment.

    *message_id* is the caller-pre-generated primary key for the row. On
    insert conflicts (watchdog racing forward_events on the same injection)
    the existing row is treated as success — idempotent by construction.
    """
    if not isinstance(content, str) or len(content) > 200_000:
        return None, "Message content exceeds the 200,000 character limit"
    # attachments must be a list — a str/dict slides past len() and then
    # _validate_attachment_url's att.get("url") AttributeErrors, taking the
    # whole message down with it (injection path: consumed but never
    # persisted, silently lost).
    if not isinstance(attachments, list):
        return None, "Message carries invalid attachments"
    if len(attachments) > 10:
        return None, "Message carries more than 10 attachments"
    if not content.strip() and not attachments:
        # Empty message: no content and no attachments — a blank user row
        # plus a full agent turn on nothing. Pure-attachment messages stay
        # legal.
        return None, "Message is empty"

    # Validate + prepare user attachments (uploaded earlier via POST
    # /api/media into the in-memory store). image_records /
    # user_attachments are clean records persisted with the message;
    # attachment_records carry derived content (inline_text /
    # image_data / rel_path) for agent.run. Store entries stay alive
    # for the duration of the turn so tools can re-read the bytes,
    # and are dropped when the turn ends — nothing is written to disk.
    from api.routers.media import get_upload
    from api.models.conversation import Conversation, Message as DbMessage
    from sqlalchemy import func, select, update as _update
    from sqlalchemy.exc import IntegrityError
    import re as _re

    media_dir = Path(fs_path) / ".tmp" / "media" / conversation_id
    attachment_records: list[dict] = []
    user_attachments: list[dict] = []
    image_records: list[dict] = []
    for att in attachments:
        # Element-level type check: the loop below calls att.get() on the
        # invalid-attachment error path too — a non-dict element would
        # AttributeError there and silently drop the whole user message.
        if not isinstance(att, dict):
            return None, "Invalid attachment"
        path = _validate_attachment_url(att, project_id, conversation_id, media_dir)
        if path is None:
            return None, f"Invalid attachment: {att.get('url', '')}"
        data = get_upload(conversation_id, path.name)
        if data is None and not path.is_file():
            return None, f"Attachment expired: {att.get('name', '')}"
        prepared = await _prepare_attachment(att, path, conversation_id, data=data)
        # .get() — attachment fields are client-supplied; a missing key
        # must not KeyError the whole turn (user message would be lost
        # before it is persisted).
        record = {k: prepared.get(k, "") for k in ("id", "url", "name", "media_type", "size")}
        if prepared.get("image_data"):
            attachment_records.append(prepared)
            image_records.append({**record, "alt": record["name"]})
        else:
            attachment_records.append(prepared)
            if str(record["media_type"]).startswith("image/"):
                # Image file but too large for vision context — still shown in UI
                image_records.append({**record, "alt": record["name"]})
            else:
                user_attachments.append(record)

    # Lock conversation row to serialize concurrent sequence number assignment
    conv_result = await db.execute(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    conv = conv_result.scalar_one_or_none()
    if conv is None or conv.status != "active":
        # Soft-deleted row still exists — a delete raced this persist (the
        # delete guard passed before the row lock). Writing the message into
        # an invisible, unrecoverable conversation would lose it silently.
        await db.rollback()
        return None, "Conversation not found"
    max_seq_result = await db.execute(
        select(func.coalesce(func.max(DbMessage.sequence_num), 0))
        .where(DbMessage.conversation_id == conversation_id)
    )
    next_seq = max_seq_result.scalar() + 1
    if message_id is None:
        message_id = str(_uuid_mod.uuid4())
    user_msg_record = DbMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        role="user",
        content=content,
        agent_slug=agent_slug,
        knowledge_refs=(
            json.dumps({"images": image_records, "attachments": user_attachments})
            if image_records or user_attachments else None
        ),
        sequence_num=next_seq,
    )
    db.add(user_msg_record)
    # Last-activity timestamp: the conversation list and /latest order by
    # COALESCE(updated_at, created_at) - a new user message is what moves an
    # old conversation back to the top of the sidebar. Bumped on the same
    # locked row/commit as the message itself.
    conv.updated_at = datetime.now(timezone.utc)
    # Auto-set conversation title from the first sentence of the first user message
    if set_title and not conv.title and content:
        first_line = content.split('\n', 1)[0].strip()
        sentence_match = _re.split(r'[。.!！?？\n]', first_line, maxsplit=1)
        title_text = (sentence_match[0].strip() if sentence_match else first_line)[:60] or content[:60]
        await db.execute(
            _update(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .values(title=title_text)
        )
    try:
        await db.commit()
    except IntegrityError:
        # Same message_id persisted by a racing path (watchdog vs
        # forward_events) — the row already exists, treat as success.
        await db.rollback()
        existing = await db.execute(
            select(DbMessage.sequence_num)
            .where(DbMessage.message_id == message_id)
        )
        seq = existing.scalar_one_or_none()
        if seq is None:
            await db.rollback()
            return None, "Message could not be persisted"
        next_seq = seq
    return _PersistUserMessageResult(
        sequence_num=next_seq,
        message_id=message_id,
        image_records=image_records,
        user_attachments=user_attachments,
        attachment_records=attachment_records,
    ), None


async def _persist_assistant_message(
    db, conversation_id: str, content: str, tool_calls: list[dict],
    message_id: str | None = None, images: list[dict] | None = None,
    files: list[dict] | None = None, *, interrupted: bool = False,
    error: bool = False,
    agent_slug: str | None = None, model_name: str | None = None,
) -> None:
    """Save an assistant message to DB after stream_end."""
    import json as _json
    import uuid as _uuid
    from sqlalchemy import func, select, update
    from api.models.conversation import Conversation, Message as DbMessage

    await db.execute(
        select(Conversation.conversation_id)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    max_seq_result = await db.execute(
        select(func.coalesce(func.max(DbMessage.sequence_num), 0))
        .where(DbMessage.conversation_id == conversation_id)
    )
    next_seq = max_seq_result.scalar() + 1
    tool_calls_json = _json.dumps(tool_calls) if tool_calls else None
    refs = {}
    if images:
        refs["images"] = images
    if files:
        refs["attachments"] = files
    if interrupted:
        # Partial output cut short by an in-flight user injection — the
        # frontend renders this as a distinct "interrupted" message.
        refs["interrupted"] = True
    if error:
        # Turn-level LLM failure persisted as an assistant row (content is
        # the error text when the turn produced nothing else) so the failure
        # survives reloads — the live error bubble is client-only.
        refs["error"] = True
    knowledge_refs_json = _json.dumps(refs) if refs else None
    msg = DbMessage(
        message_id=message_id or str(_uuid.uuid4()),
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        agent_slug=agent_slug,
        # model_name is a String(100) column — cap like agent_tasks
        # actual_model so an overlong model id cannot DataError the persist.
        model_name=(model_name or "")[:100] or None,
        tool_calls=tool_calls_json,
        knowledge_refs=knowledge_refs_json,
        sequence_num=next_seq,
    )
    db.add(msg)
    # Last-activity timestamp (same contract as the user-message persist):
    # the reply landing in history is also "the conversation was just used".
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(updated_at=datetime.now(timezone.utc))
    )
    await db.commit()


def _upsert_file_records(buf: list[dict], files: list[dict]) -> None:
    """Merge file records into the turn buffer, keyed by name.

    Re-delivering an already-delivered file (a regeneration overwriting
    web-slides.html, a re-export of report.csv) must update the existing
    link, not append a second one - otherwise one deliverable shows up as
    one download link per rewrite.
    """
    for f in files:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        for i, existing in enumerate(buf):
            if existing.get("name") == f["name"]:
                buf[i] = f
                break
        else:
            buf.append(f)


def _norm_task_id(task_id: str | None) -> str | None:
    """Cap LLM-generated task IDs to the 36-char column width.

    agent_tasks.task_id and task_events.task_id are String(36). Planner ids
    are usually "task-1"-style, but the LLM can emit arbitrarily long ids —
    on MSSQL an overlong id truncate-crashes the persist batch (DataError)
    and drops the whole task plan. Capping keeps events and rows in sync
    (both sides apply the same rule).
    """
    if not task_id:
        return task_id
    return str(task_id)[:36]


def _bounded_json(payload: dict, limit: int = 8000) -> str:
    """Serialize *payload* into VALID JSON that fits the DB column.

    Byte-slicing the serialized output would cut mid-string and persist
    corrupt JSON — every later reader does json.loads, and one bad row 500s
    the whole task-events timeline. So the source is trimmed instead: a cut
    string VALUE is still valid JSON.
    """
    import json as _json
    encoded = _json.dumps(payload, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded

    def _trim(value, budget: int):
        if isinstance(value, str):
            return value[:budget] if len(value) > budget else value
        if isinstance(value, list):
            return [_trim(x, max(budget // 2, 32)) for x in value[:40]]
        if isinstance(value, dict):
            return {k: _trim(v, max(budget // 2, 32)) for k, v in value.items()}
        return value

    trimmed = _json.dumps(_trim(payload, limit // 8), ensure_ascii=False)
    if len(trimmed) <= limit:
        return trimmed
    # Last resort: minimal valid envelope — the event still shows in the
    # timeline; the oversized data is dropped rather than persisted corrupt.
    return _json.dumps(
        {k: payload[k] for k in ("task_id", "status", "summary", "error") if k in payload},
        ensure_ascii=False,
    )


async def _persist_task_event(db, conversation_id: str, event_type: str, task_id: str | None, payload: dict) -> tuple[str, int]:
    """Append a durable TaskEvent and return (event_id, sequence)."""
    import uuid as _uuid
    from sqlalchemy import func, select
    from api.models.task_event import TaskEvent

    max_seq = await db.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0))
        .where(TaskEvent.conversation_id == conversation_id)
    )
    next_seq = max_seq.scalar() + 1
    eid = str(_uuid.uuid4())
    bounded_payload = _bounded_json(payload)
    evt = TaskEvent(
        event_id=eid,
        conversation_id=conversation_id,
        sequence=next_seq,
        event_type=event_type,
        task_id=_norm_task_id(task_id),
        payload=bounded_payload,
    )
    db.add(evt)
    await db.commit()
    return eid, next_seq


async def _persist_terminal_task_event(db, conversation_id: str, event) -> None:
    """Persist a terminal task event (task_completed/task_failed/task_skipped)
    to the AgentTask row + TaskEvent log.

    Shared by forward_events and the post-drain-timeout drain so a task's
    final status is written exactly once, whichever path wins.
    """
    task_id = event.data.get("task_id", "")
    if event.type == "task_completed":
        await _update_task_status(
            db, conversation_id, task_id, "completed",
            result_summary=event.data.get("summary", ""),
        )
    elif event.type == "task_failed":
        await _update_task_status(
            db, conversation_id, task_id, "failed",
            error_message=event.data.get("error", ""),
        )
    elif event.type == "task_skipped":
        await _update_task_status(
            db, conversation_id, task_id, "skipped",
            error_message=event.data.get("error", ""),
        )
    await _persist_task_event(db, conversation_id, event.type, task_id, event.data)


async def _persist_tasks(db, conversation_id: str, tasks: list[dict]) -> None:
    """Bulk insert AgentTask rows on task_plan_created."""
    import json as _json
    import uuid
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from api.models.conversation import AgentTask

    for idx, task in enumerate(tasks):
        # Events use the planner's task ID, so persist that same ID. Appending
        # a suffix breaks real-time task updates after history is reloaded.
        planner_id = str(_norm_task_id(task.get("id") or uuid.uuid4()))
        # task_id is a GLOBAL primary key (not per-conversation). Planner ids
        # are deterministic ("task-1" style), so two conversations planning
        # tasks collide and the second INSERT fails the whole batch — the
        # plan AND its task_plan_created event are silently lost. Reuse the
        # planner id only when it is free GLOBALLY (the dedup check
        # was scoped to this conversation, so a row owned by another
        # conversation was invisible and the collision still killed the
        # batch); otherwise fall back to a fresh UUID so the plan survives.
        # (Real-time updates for that one fallback task miss the DB row until
        # reload — a narrow trade-off vs. dropping the entire plan.)
        existing = (await db.execute(
            select(AgentTask.task_id).where(
                AgentTask.task_id == planner_id,
            ).limit(1)
        )).first()
        task_id = planner_id if existing is None else str(uuid.uuid4())
        # Length guards: title is Unicode(500), tools_needed/depends_on are
        # String(500), estimated_complexity String(20) — LLM-generated values
        # over the column limit raise a DataError on SQL Server.
        tools = task.get("tools_needed")
        depends = task.get("depends_on")
        tools_str = _json.dumps(tools, ensure_ascii=False) if isinstance(tools, (list, dict)) else (str(tools) if tools is not None else None)
        depends_str = _json.dumps(depends, ensure_ascii=False) if isinstance(depends, (list, dict)) else (str(depends) if depends is not None else None)
        complexity = task.get("estimated_complexity")
        record = AgentTask(
            task_id=task_id,
            conversation_id=conversation_id,
            sequence_num=idx,
            title=(task.get("title") or "")[:500],
            status="pending",
            estimated_complexity=(str(complexity)[:20] if complexity else None),
            tools_needed=(tools_str[:500] if tools_str else None),
            depends_on=(depends_str[:500] if depends_str else None),
        )
        try:
            async with db.begin_nested():
                db.add(record)
                await db.flush()
        except IntegrityError:
            # Race between the global dedup check above and this flush (two
            # conversations planning "task-1" simultaneously): re-key with a
            # UUID and retry inside a fresh savepoint.
            task_id = str(uuid.uuid4())
            record.task_id = task_id
            async with db.begin_nested():
                db.add(record)
                await db.flush()
    await db.commit()


async def _update_task_status(
    db,
    conversation_id: str,
    task_id: str,
    status: str,
    result_summary: str | None = None,
    error_message: str | None = None,
    current_step: str | None = None,
    next_step: str | None = None,
    progress_completed: int | None = None,
    progress_total: int | None = None,
    actual_model: str | None = None,
) -> None:
    """Update status (and optional summary/error/progress) for a single AgentTask.

    Filtered by conversation_id: task_ids are LLM-generated and routinely
    collide across conversations ("task-1"), and the task_id PK is not
    globally unique — without the filter, one conversation's events would
    rewrite another conversation's task rows.
    """
    import datetime
    from sqlalchemy import update
    from api.models.conversation import AgentTask

    values: dict = {"status": status}
    if status == "running":
        values["started_at"] = datetime.datetime.now(datetime.timezone.utc)
    elif status in ("completed", "failed", "skipped"):
        values["completed_at"] = datetime.datetime.now(datetime.timezone.utc)
    if result_summary is not None:
        values["result_summary"] = result_summary[:2000]
    if error_message is not None:
        values["error_message"] = error_message[:2000]
    if current_step is not None:
        values["current_step"] = current_step[:500]
    if next_step is not None:
        values["next_step"] = next_step[:500]
    if progress_completed is not None:
        values["progress_completed"] = progress_completed
    if progress_total is not None:
        values["progress_total"] = progress_total
    if actual_model is not None:
        values["actual_model"] = actual_model[:100]

    await db.execute(
        update(AgentTask)
        .where(
            AgentTask.conversation_id == conversation_id,
            AgentTask.task_id == _norm_task_id(task_id),
        )
        .values(**values)
    )
    await db.commit()


async def _update_task_progress(db, conversation_id: str, task_id: str, data: dict) -> None:
    """Persist task_progress event fields to the AgentTask row."""
    from sqlalchemy import update
    from api.models.conversation import AgentTask

    if not task_id:
        return
    values: dict = {}
    if data.get("current_step") is not None:
        values["current_step"] = str(data["current_step"])[:500]
    if data.get("next_step") is not None:
        values["next_step"] = str(data["next_step"])[:500]
    if data.get("progress_completed") is not None:
        values["progress_completed"] = int(data["progress_completed"])
    if data.get("progress_total") is not None:
        values["progress_total"] = int(data["progress_total"])
    if not values:
        return
    await db.execute(
        update(AgentTask)
        .where(
            AgentTask.conversation_id == conversation_id,
            AgentTask.task_id == _norm_task_id(task_id),
        )
        .values(**values)
    )
    await db.commit()


async def _scan_pending_task_files(
    db, conversation_id: str, project_id: str, project_fs_path: str
) -> str:
    """Scan for temp files from pending/running tasks and return their content."""
    from pathlib import Path
    from sqlalchemy import select
    from api.models.conversation import AgentTask

    result = await db.execute(
        select(AgentTask).where(
            AgentTask.conversation_id == conversation_id,
            AgentTask.status.in_(["running", "pending"]),
        )
    )
    pending_tasks = result.scalars().all()
    if not pending_tasks:
        return ""

    conv_media = Path(project_fs_path) / ".tmp" / "media" / conversation_id
    parts = await asyncio.to_thread(_read_task_files_sync, str(conv_media))
    if not parts:
        return ""
    return "## Pending Task Files\n\n" + "\n\n".join(parts)


# ── Attachment handling ─────────────────────────────────────────────────
#
# User attachments are uploaded via POST /api/media (stored in the
# conversation's .tmp/media dir), then referenced from the WS message frame.
# The helpers below validate client-supplied refs and re-derive file content
# (inline text / image base64) from disk — content is never persisted in the
# message row, only references.

_MEDIA_URL_MARKER = "/api/media/"
_MAX_INLINE_TEXT_CHARS = 20_000       # inline text cap per attachment
_MAX_VISION_IMAGE_BYTES = 3 * 1024 * 1024   # larger images stay out of context
_REHYDRATE_IMAGE_TURNS = 4            # only the last N attachment turns get vision
_TEXTISH_SUFFIXES = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".tsx",
    ".vue", ".html", ".css", ".yaml", ".yml", ".toml", ".xml", ".sh",
    ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".ini", ".cfg",
}


def _validate_attachment_url(att: dict, project_id: str, conversation_id: str, media_dir: Path) -> Path | None:
    """Return the resolved attachment path, or None.

    Security gate for client-supplied refs: the media URL's project and
    conversation must match the current conversation, and the filename must
    resolve inside the conversation's media dir. The file itself may live in
    the in-memory upload store (user uploads are never written to disk).
    """
    # attachments is checked as a list by callers, but its ELEMENTS are
    # client-supplied too — a non-dict element (str/int/None) would
    # AttributeError on att.get below and take the whole turn down
    # (message consumed but never persisted).
    if not isinstance(att, dict):
        return None
    from api.routers.media import _FILENAME_RE, get_upload
    url = str(att.get("url") or "")
    marker = url.find(_MEDIA_URL_MARKER)
    if marker < 0:
        return None
    segs = url[marker + len(_MEDIA_URL_MARKER):].split("/")
    if len(segs) != 3:
        return None
    pid, cid, fname = segs
    if pid != project_id or cid != conversation_id:
        return None
    if ".." in fname or not _FILENAME_RE.match(fname):
        return None
    base = media_dir.resolve()
    path = (base / fname).resolve()
    if not path.is_relative_to(base):
        return None
    if path.is_file() or get_upload(conversation_id, fname) is not None:
        return path
    return None


def _attachment_ref_text(att: dict, conversation_id: str) -> str:
    """Text representation of an attachment (name + relative path) for the LLM."""
    name = att.get("name", "unknown")
    media_type = att.get("media_type", "")
    size = att.get("size", 0)
    fname = Path(str(att.get("url", ""))).name or name
    return f"Attachment: {name} ({media_type}, {size} bytes) — file path: .tmp/media/{conversation_id}/{fname}"


async def _prepare_attachment(att: dict, media_path: Path, conversation_id: str, data: bytes | None = None) -> dict:
    """Read inline text / image base64 for an attachment.

    Content is re-derived per turn — nothing here is persisted. When the
    attachment is a user upload, `data` carries the in-memory bytes: any
    UTF-8-decodable file (whatever the suffix — .ps1, .bat, ...) gets up to
    _MAX_INLINE_TEXT_CHARS chars inline, images up to _MAX_VISION_IMAGE_BYTES
    get base64 data, binary files degrade to a path reference. Disk files
    (data is None) keep the old behavior: only known text-ish / image
    suffixes are read.
    """
    from api.routers.media import _IMAGE_SUFFIXES, _MIME_BY_SUFFIX
    prepared = {**att, "rel_path": f".tmp/media/{conversation_id}/{media_path.name}"}
    suffix = media_path.suffix.lower()
    if data is None:
        if not media_path.is_file():
            return prepared  # degraded to a path reference
        if suffix in _TEXTISH_SUFFIXES:
            try:
                data = await asyncio.to_thread(media_path.read_bytes)
            except OSError:
                data = None
        elif suffix in _IMAGE_SUFFIXES:
            try:
                data = await asyncio.to_thread(media_path.read_bytes)
            except OSError:
                data = None
    if data is not None:
        if suffix not in _IMAGE_SUFFIXES:
            # Uploads arrive with bytes in hand — inline any UTF-8 file so
            # the agent gets the content without a tool read.
            try:
                text = data.decode("utf-8")
                if len(text) > _MAX_INLINE_TEXT_CHARS:
                    text = text[:_MAX_INLINE_TEXT_CHARS] + "\n[... truncated ...]"
                prepared["inline_text"] = text
            except UnicodeDecodeError:
                pass  # binary (xlsx, zip, ...) — falls back to a path reference
        elif len(data) <= _MAX_VISION_IMAGE_BYTES:
            prepared["image_data"] = base64.b64encode(data).decode("ascii")
            prepared["image_media_type"] = _MIME_BY_SUFFIX.get(suffix, "image/png")
    return prepared


# --- In-flight message injection -------------------------------------------
#
# A user message arriving mid-turn (session already registered) is enqueued as
# an injection the agent consumes at its next step boundary. Shared by the WS
# receive loop and the run_turn claim-window drain, so these live in the turn
# kernel rather than the WS shell.

async def _enqueue_injected_message(
    conversation_id: str, session, msg: dict
) -> None:
    """Enqueue a user message as an injection on the running session.

    Replies to the current WS with input_queued (or an error when the
    message fails validation or the queue is full). The agent consumes the
    entry at its next step boundary; the watchdog persists it if the turn
    ends first.
    """
    from agent_core.session import UserInputEntry

    content = msg.get("content", "")
    attachments = msg.get("attachments") or []
    if not isinstance(content, str) or len(content) > 200_000:
        await manager.send(conversation_id, {
            "type": "input_rejected", "message_id": None, "content": content,
            "message": "Message content exceeds the 200,000 character limit",
        })
        return
    # attachments must be a list — a str/dict slides past len() and then
    # _validate_attachment_url's att.get("url") AttributeErrors, taking the
    # whole message down with it (injection path: consumed but never
    # persisted, silently lost).
    if not isinstance(attachments, list):
        await manager.send(conversation_id, {
            "type": "input_rejected", "message_id": None, "content": content,
            "message": "Message carries invalid attachments",
        })
        return
    if len(attachments) > 10:
        await manager.send(conversation_id, {
            "type": "input_rejected", "message_id": None, "content": content,
            "message": "Message carries more than 10 attachments",
        })
        return
    entry = UserInputEntry(
        message_id=str(_uuid_mod.uuid4()),
        content=content,
        attachments=attachments,
        agent_id=msg.get("agent_id"),
        config_id=msg.get("config_id"),
    )
    entry.persisted = asyncio.get_running_loop().create_future()
    if not session.enqueue_user_input(entry):
        await manager.send(conversation_id, {
            "type": "input_rejected", "message_id": None, "content": content,
            "message": "Input queue is full. Wait for the agent to finish.",
        })
        return
    # Watchdog: if the turn ends before the agent consumes this entry, it is
    # persisted as an ordinary user message so the user's words are never
    # lost. Watches THIS session — a new turn's session gets its own input
    # queue and could never consume this entry.
    _spawn_background(_guard_injected_message(conversation_id, entry, session))
    await manager.send(conversation_id, {
        "type": "input_queued",
        "message_id": entry.message_id,
        "content": content,
    })


async def _guard_injected_message(
    conversation_id: str, entry, session
) -> None:
    """Persist an injected message as an ordinary user message when the turn
    ends before the agent consumed it.

    The agent consumes queued input only at step boundaries; a message sent
    just as the turn is finishing stays queued. This watchdog waits for the
    owning session to be deregistered (the turn ended), and if the entry was
    never consumed, falls back to normal persistence and tells the client
    with input_not_processed. Idempotency comes from the pre-generated
    message_id PK — racing the forward_events persist of the same message is
    safe either way.
    """
    try:
        # The session is deregistered when _handle_message unwinds. Waiting
        # for ANY session would wait on the next turn's session too — it has
        # its own input queue and can never consume this entry, so the
        # orphan would sit until the conversation went fully idle.
        while True:
            if entry.consumed:
                return
            sess = manager.get_session(conversation_id)
            if sess is None or sess is not session:
                break
            await asyncio.sleep(0.2)
        if entry.consumed:
            return
        await _persist_orphan_injection(conversation_id, entry)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "Injection watchdog failed for %s message_id=%s",
            conversation_id, entry.message_id, exc_info=True,
        )


async def _persist_orphan_injection(conversation_id: str, entry) -> None:
    """Best-effort persistence of an unconsumed injection, then notify the
    client with input_not_processed."""
    from api.database import AsyncSessionLocal
    from api.models.conversation import Conversation
    from api.paths import resolve_project_fs_path
    from sqlalchemy import select

    async with AsyncSessionLocal() as gdb:
        result = await gdb.execute(
            select(Conversation.project_id)
            .where(Conversation.conversation_id == conversation_id)
        )
        project_id = result.scalar_one_or_none()
        if project_id is None:
            return
        fs_path = await resolve_project_fs_path(str(project_id), gdb)
        if not fs_path:
            return
        # Attachments expire with the turn: this watchdog's persist races the
        # turn's finally drop_uploads, and a lost upload would otherwise fail
        # the whole persist ("Attachment expired") — taking the message text
        # down with it. Filter out unresolvable attachments so the user's
        # words are still persisted; the remaining ones stay attached.
        from api.routers.media import get_upload
        from pathlib import Path as _Path
        media_dir = _Path(fs_path) / ".tmp" / "media" / conversation_id
        survivors = []
        for att in entry.attachments or []:
            path = _validate_attachment_url(att, str(project_id), conversation_id, media_dir)
            if path is None:
                continue
            if get_upload(conversation_id, path.name) is not None or path.is_file():
                survivors.append(att)
        _, persist_err = await _prepare_and_persist_user_message(
            gdb, conversation_id, str(project_id), str(fs_path),
            entry.content, survivors,
            set_title=False, message_id=entry.message_id,
            agent_slug=entry.agent_id,
        )
    await manager.send(conversation_id, {
        "type": "input_not_processed",
        "message_id": entry.message_id,
        "content": entry.content,
        "message": "Agent finished before the message was processed." if persist_err is None else persist_err,
    })


async def _save_secret_from_response(
    conversation_id: str, user_id: str, msg: dict
) -> bool:
    """Save a secret value from user_selection_response to project_secrets.

    Returns True on success, False on failure.
    The plaintext is never forwarded to the agent session.
    """
    from api.database import AsyncSessionLocal
    from api.models.conversation import Conversation
    from api.models.project_secret import ProjectSecret
    from api.services.token_vault import encrypt_project_secret, key_hint
    from sqlalchemy import or_, select

    service_key = msg.get("service_key", "")
    # Normalize a missing environment to "" so the unique constraint can
    # enforce uniqueness (SQL Server unique indexes treat NULL as distinct).
    environment = msg.get("environment") or ""
    value = msg.get("value", "")
    if not service_key or not value:
        return False

    try:
        async with AsyncSessionLocal() as db:
            # Look up project_id from the conversation
            result = await db.execute(
                select(Conversation.project_id).where(
                    Conversation.conversation_id == conversation_id
                )
            )
            row = result.first()
            if not row:
                return False
            project_id = str(row[0])

            # Upsert by unique key — must check ALL rows (not just is_active=True)
            # so that a previously soft-deleted row doesn't block the INSERT.
            from datetime import datetime, timezone
            # Match legacy NULL rows too — they predate the "" normalization;
            # SQL Server unique indexes treat NULL as distinct, so both can
            # exist for the same key. Prefer the "" row and collapse stale
            # NULL duplicates onto it.
            if environment:
                env_clause = ProjectSecret.environment == environment
            else:
                env_clause = or_(
                    ProjectSecret.environment.is_(None),
                    ProjectSecret.environment == "",
                )
            existing = await db.execute(
                select(ProjectSecret).where(
                    ProjectSecret.project_id == project_id,
                    ProjectSecret.service_key == service_key,
                    env_clause,
                    ProjectSecret.secret_name == "default",
                ).with_for_update()
            )
            secrets = existing.scalars().all()
            secret = None
            if secrets:
                secret = next(
                    (s for s in secrets if s.environment == environment),
                    secrets[0],
                )
                for stale in secrets:
                    if stale is not secret:
                        await db.delete(stale)
            encrypted = encrypt_project_secret(value, project_id)
            hint = key_hint(value)

            if secret:
                secret.environment = environment  # migrate a lone legacy NULL row to ""
                secret.encrypted_value = encrypted
                secret.key_hint = hint
                secret.updated_by = user_id
                secret.updated_at = datetime.now(timezone.utc)
                secret.is_active = True
            else:
                secret = ProjectSecret(
                    project_id=project_id,
                    service_key=service_key,
                    environment=environment,
                    secret_name="default",
                    encrypted_value=encrypted,
                    key_hint=hint,
                    created_by=user_id,
                )
                db.add(secret)
            await db.commit()
            return True
    except Exception:
        _log.warning("Failed to save secret from user response", exc_info=True)
        return False


async def _save_user_token_from_response(
    user_id: str, msg: dict
) -> bool:
    """Save a secret value from user_selection_response to user_tokens.

    Returns True on success, False on failure.
    The plaintext is never forwarded to the agent session.
    """
    from api.database import AsyncSessionLocal
    from api.models.user import UserToken
    from api.services.token_vault import encrypt, key_hint
    from sqlalchemy import select

    service_key = msg.get("service_key", "")
    value = msg.get("value", "")
    if not service_key or not value:
        return False

    try:
        async with AsyncSessionLocal() as db:
            from datetime import datetime, timezone

            existing = await db.execute(
                select(UserToken).where(
                    UserToken.user_id == user_id,
                    UserToken.service_key == service_key,
                ).with_for_update()
            )
            token = existing.scalar_one_or_none()
            encrypted = encrypt(value, user_id)
            hint = key_hint(value)

            if token:
                token.encrypted_value = encrypted
                token.key_hint = hint
                token.updated_at = datetime.now(timezone.utc)
            else:
                token = UserToken(
                    user_id=user_id,
                    service_key=service_key,
                    encrypted_value=encrypted,
                    key_hint=hint,
                )
                db.add(token)
            await db.commit()
            return True
    except Exception:
        _log.warning("Failed to save user token from user response", exc_info=True)
        return False
