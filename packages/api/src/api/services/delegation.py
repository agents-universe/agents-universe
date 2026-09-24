"""Agent-to-agent delegation: run a sub-turn and hand its result back.

An agent that lacks a capability (a tool it does not have, a domain it does not
know) calls ``delegate_agent``. That tool lazily imports this module and waits
on :func:`run_delegated_turn`, which runs the named agent as a **nested turn of
the same conversation**: same turn claim, same registered session, inline in the
calling task. The child's reply is persisted as an ordinary assistant message
carrying its own ``agent_slug`` (so the UI attributes it), a compact summary is
returned to the caller, and the parent then finishes its own reply.

Three things make that safe, and all three are load-bearing:

* **``run_turn`` is called with ``delegation=``** — its nested-mode guards keep
  the child away from every conversation-scoped singleton (turn claim,
  registered session, run record, uploads, pending injections, task rows).
* **The child's events go through :class:`DelegationTransport`**, which is
  fail-closed: only an explicit whitelist reaches the client. Anything else
  would land on the *parent's* bubble or task panel, because once a turn's
  events are forwarded the browser cannot tell which agent produced them.
* **The whole child turn holds the conversation's delegation gate**, so two
  parallel plan tasks cannot interleave their message inserts (``sequence_num``
  is ``max+1`` under a row lock, and SQLite ignores ``FOR UPDATE``).

The child runs inline rather than in its own ``asyncio.Task``: that keeps
contextvars, cancellation and the tool-call stack in one place, so a user's Stop
tears down parent and child together. It also means a child must never *swallow*
a cancellation it did not cause — see the ``CancelledError`` arm of
``agent_turn.run_turn``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from api.websocket.manager import manager

_log = logging.getLogger("agents_universe.ws")

#: Events a delegated turn may push to the client verbatim.
#:
#: ``user_selection_*`` is the confirmation dialog (a delegated agent may still
#: ask the user to approve a push or a delete); ``image_output``/``file_output``
#: are its deliverables, which must reach the chat; the ``knowledge_*`` trio is
#: the progress indicator for its on-demand knowledge reads.
#:
#: A whitelist, not a blacklist: ``forward_events`` re-sends every session event
#: as ``{"type": ..., **data}``, and the browser cannot tell a child's event
#: from the parent's — so an unlisted event would freeze the parent's bubble
#: (``stream_end``), clear its streaming state and pending dialogs
#: (``abort_ack``), replace its task panel (``task_plan_created``), or double
#: count its tokens (``token_update``).
#:
#: ``knowledge_loaded`` is deliberately absent: the client treats it as the
#: conversation's authoritative loaded-files list, so a child's load would
#: overwrite the parent's. ``knowledge_updated`` carries no payload (the client
#: re-fetches), which makes it safe to forward.
#:
#: ``turn_status`` IS forwarded: while the parent blocks on the delegate tool
#: call, the child's phase frames (and its heartbeat re-sends) are the only
#: visibility into the nested turn, and a phase string carries no state that
#: could clobber the parent's bubble/plan/tokens. ``thinking_delta`` /
#: ``thinking_end`` stay OUT — the child's reasoning must not enter the
#: parent's streaming bubble (same rationale as ``stream_delta`` being
#: captured but not forwarded); the child persists its own thinking row.
_FORWARDED_EVENTS = frozenset({
    "user_selection_required",
    "user_selection_cancelled",
    "image_output",
    "file_output",
    "knowledge_updated",
    "knowledge_dynamic_load",
    "knowledge_dynamic_unload",
    "turn_status",
})

#: Characters of the child's streamed reply kept as the summary handed back to
#: the delegating agent. The full reply is in the transcript; this is a
#: pointer-with-substance, not a second copy of it.
_SUMMARY_LIMIT = 4000

#: Defaults, overridable from settings. Duplicated here so the module stays
#: usable (and testable) without a configured app.
DEFAULT_MAX_DEPTH = 2
DEFAULT_TIMEOUT_SECONDS = 1800.0


@dataclass(frozen=True)
class DelegationContext:
    """Immutable delegation state travelling with one turn.

    Stored on ``ToolContext.delegation``. ``agent-core`` treats it as opaque —
    it is built here and in ``agent_turn``, and read back here on a tool call.

    ``chain`` runs outermost-first and ALWAYS ends with the agent that is
    currently running, so ``depth = len(chain) - 1`` and ``chain[-2]`` is the
    parent a child reports back to. ``parent_session`` is the top-level session:
    the only one registered with the connection manager, and therefore the only
    route by which a user's answer to a child's question can be resolved.
    ``holds_gate`` records that this chain already owns the conversation's
    delegation gate — a descendant must not re-enter it, because its ancestor
    holds it for the entire child turn.
    """

    chain: tuple[str, ...]
    parent_session: Any
    actor_user_id: str
    holds_gate: bool = False
    #: The delegating agent's ``delegates_to`` allowlist; empty means "any".
    policy: tuple[str, ...] = ()
    #: Set by the delegator to stop this turn (a child that blew its timeout):
    #: nothing else can reach the turn's agent task from outside.
    cancel_event: asyncio.Event | None = None


class DelegationTransport:
    """Fail-closed event sink for a nested turn.

    Captures what the delegator needs (the child's reply text, its message id,
    its token count, whether it failed or was stopped) and forwards only
    :data:`_FORWARDED_EVENTS` onward. ``send`` returns True for dropped events
    as well: the return value only tells the turn whether a *live client* got
    the event, and a nested turn's persistence decisions do not depend on it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.parts: list[str] = []
        self.message_id: str | None = None
        self.tokens_used: int = 0
        self.error: str | None = None
        self.stop_reason: str | None = None

    @property
    def summary(self) -> str:
        text = "".join(self.parts).strip()
        if len(text) > _SUMMARY_LIMIT:
            # Keep the head and the tail: an agent's conclusion is usually at
            # the end, its framing at the start.
            half = _SUMMARY_LIMIT // 2
            return f"{text[:half]}\n...\n{text[-half:]}"
        return text

    @property
    def status(self) -> str:
        """Outcome of the child turn, as reported to the delegating agent."""
        if self.stop_reason in ("aborted", "interrupted"):
            return "aborted"
        if self.error:
            return "error"
        return "ok"

    async def send(self, conversation_id: str, data: dict) -> bool:
        event_type = data.get("type")
        # Capture first: several of these are dropped afterwards, and they are
        # exactly the ones carrying the result.
        if event_type == "stream_delta":
            self.parts.append(str(data.get("delta") or ""))
        elif event_type == "stream_end":
            if data.get("message_id"):
                self.message_id = str(data["message_id"])
            self.tokens_used = int(data.get("total_tokens") or 0)
            self.stop_reason = data.get("stop_reason")
        elif event_type == "error":
            self.error = str(data.get("message") or "error")

        if event_type not in _FORWARDED_EVENTS:
            return True
        try:
            await self._inner.send(conversation_id, data)
        except Exception:
            # A dead socket must not fail the turn — the same tolerance the
            # turn's own transport path has.
            _log.debug("Delegation transport forward failed", exc_info=True)
        return True


def _settings_guard() -> tuple[bool, int, float]:
    """Return (enabled, max_depth, timeout_seconds) from app settings."""
    try:
        from api.config import get_settings

        s = get_settings()
    except Exception:  # pragma: no cover - settings are always available in-app
        return True, DEFAULT_MAX_DEPTH, DEFAULT_TIMEOUT_SECONDS
    return (
        bool(getattr(s, "agent_delegation_enabled", True)),
        int(getattr(s, "agent_delegation_max_depth", DEFAULT_MAX_DEPTH)),
        float(getattr(s, "agent_delegation_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
    )


def _resolve_definition(context: Any, slug: str) -> str | None:
    """Resolve *slug* to the definition file the runtime would load.

    Project workspace first, global second — the same order
    ``resolve_agent_definition_path`` uses, so an advertised slug always
    resolves. A project agent of another project simply is not found here:
    isolation is structural, not a filter.
    """
    from api.paths import AGENTS_DIR
    from api.services.agent_sync import resolve_agent_definition_path

    project_fs_path = getattr(context, "project_fs_path", None)
    if project_fs_path:
        try:
            path = resolve_agent_definition_path(slug, project_fs_path)
        except ValueError:
            _log.warning("Delegation refused: unsafe slug %r", slug)
            return None
        if path:
            return path
    candidate = AGENTS_DIR / f"{slug}.agent.md"
    if candidate.is_file():
        return str(candidate)
    if "--" in slug:
        # The `{project}--{name}` form means a project-scoped agent: reaching
        # here, it belongs to a different project (CLAUDE.md #6).
        _log.warning("Delegation refused: %s is not an agent of this project", slug)
    return None


async def run_delegated_turn(
    context: Any,
    *,
    agent_slug: str,
    brief: str,
    reason: str,
) -> dict[str, Any]:
    """Run *agent_slug* as a nested turn and return a compact result.

    Never raises for a *delegation* problem (unknown agent, depth exceeded,
    feature disabled, timeout, child failure): the calling tool must always get
    a dict it can report to the model. Cancellation and genuine bugs propagate.
    """
    ctx = getattr(context, "delegation", None)
    if ctx is None:
        return _result("refused", str(agent_slug or ""), error="This run cannot delegate.")

    enabled, max_depth, timeout = _settings_guard()
    if not enabled:
        return _result("refused", str(agent_slug or ""), error="Delegation is disabled on this server.")

    slug = str(agent_slug or "").strip()
    if not slug:
        return _result("refused", "", error="No target agent was given.")

    chain = tuple(getattr(ctx, "chain", ()) or ())
    depth = max(len(chain) - 1, 0)
    if slug in chain:
        # Also covers self-delegation: the running agent is chain[-1].
        return _result(
            "refused", slug,
            error=f"{slug} is already handling this task further up the chain.",
        )
    if depth + 1 > max_depth:
        return _result(
            "refused", slug,
            error=(
                f"Maximum delegation depth ({max_depth}) reached. "
                "Finish the subtask yourself, or report what you could not do."
            ),
        )

    policy = tuple(getattr(ctx, "policy", ()) or ())
    if policy and slug not in policy:
        return _result(
            "refused", slug,
            error=f"This agent may only delegate to: {', '.join(policy)}.",
        )

    app = getattr(context, "app", None)
    if app is None:
        return _result("refused", slug, error="Delegation is unavailable in this environment.")

    definition_path = _resolve_definition(context, slug)
    if definition_path is None:
        return _result(
            "refused", slug,
            error=(
                f"No agent named {slug!r} is available here. "
                "Call list_agents to see the agents this project can delegate to."
            ),
        )

    # The child's own `delegates_to` governs what IT may delegate on to.
    from agent_core.delegation import read_delegation_policy

    child_policy = read_delegation_policy(definition_path)
    display_name = _display_name(definition_path) or slug

    from .agent_turn import run_turn

    conversation_id = context.conversation_id
    cancel_event = asyncio.Event()
    child_ctx = DelegationContext(
        chain=chain + (slug,),
        parent_session=ctx.parent_session,
        actor_user_id=ctx.actor_user_id,
        holds_gate=True,
        policy=child_policy,
        cancel_event=cancel_event,
    )
    # `manager` is the inner sink: it implements the same send contract as the
    # turn's transports, addressing the conversation's live socket.
    transport = DelegationTransport(manager)

    # Every nested turn of one conversation runs one at a time (see the module
    # docstring). Only the outermost delegation acquires: a grandchild inherits
    # the already-held gate, because re-acquiring it would deadlock against the
    # ancestor holding it for the whole of this turn.
    gate = None if getattr(ctx, "holds_gate", False) else manager.delegation_gate(conversation_id)
    started = time.monotonic()

    await _emit(ctx, "delegate_started", {
        "agent": slug,
        "agent_name": display_name,
        "reason": reason,
        "brief": brief,
        "depth": depth + 1,
    })
    _log.info(
        "Delegating to %s (depth %d) in conversation %s: %s",
        slug, depth + 1, conversation_id, (reason or "")[:200],
    )

    timer: asyncio.Task | None = None
    if gate is not None:
        await gate.acquire()
    try:
        if timeout > 0:
            timer = asyncio.create_task(_expire(timeout, cancel_event))
        await run_turn(
            conversation_id=conversation_id,
            # run_turn expects a socket-like object exposing `.app`, and reads
            # the registries off `ws.app.state`; `manager` is unused when an
            # explicit transport is supplied (see agent_turn's transport setup).
            ws=SimpleNamespace(app=app),
            msg={"content": brief, "agent_id": slug},
            user_id=ctx.actor_user_id,
            transport=transport,
            # A child may ask the user to confirm a push or a delete, but only
            # when its parent could have: a headless run must degrade those
            # prompts to errors instead of blocking on an answer nobody can
            # give.
            interactive=bool(getattr(context, "interactive", True)),
            actor_user_id=ctx.actor_user_id,
            delegation=child_ctx,
        )
    except asyncio.CancelledError:
        # The parent turn is being torn down: stop the child instead of letting
        # it keep burning tokens with nobody left to report to, then let the
        # cancellation continue outward.
        cancel_event.set()
        await _emit(ctx, "delegate_finished", {
            "agent": slug,
            "status": "aborted",
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        raise
    except Exception as exc:
        _log.exception("Delegated turn for %s failed", slug)
        await _emit(ctx, "delegate_finished", {
            "agent": slug,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        return _result(
            "error", slug, agent_name=display_name,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"The delegated turn failed ({type(exc).__name__}).",
        )
    finally:
        if timer is not None:
            timer.cancel()
        if gate is not None:
            gate.release()

    duration_ms = int((time.monotonic() - started) * 1000)
    # Only the timeout path can have set the event on a clean return: the
    # parent-teardown path sets it and re-raises.
    status = "timeout" if cancel_event.is_set() else transport.status

    # The child's reply landed in the transcript under its own agent_slug; tell
    # the client to reload history so it appears with its attribution badge.
    await _emit(ctx, "conversation_updated", {})
    await _emit(ctx, "delegate_finished", {
        "agent": slug,
        "agent_name": display_name,
        "status": status,
        "message_id": transport.message_id,
        "tokens_used": transport.tokens_used,
        "duration_ms": duration_ms,
        "error": transport.error,
    })

    result = _result(
        status, slug,
        agent_name=display_name,
        summary=transport.summary,
        message_id=transport.message_id,
        tokens_used=transport.tokens_used,
        duration_ms=duration_ms,
        error=transport.error,
    )
    if status == "timeout":
        result["error"] = (
            f"The agent ran past its {int(timeout)}s limit and was stopped. "
            "Its partial reply is in the conversation."
        )
    if not result.get("summary"):
        result["note"] = (
            "The agent produced no text. Its reply may be in an earlier message "
            "of this conversation — check the transcript before retrying."
        )
    return result


async def _expire(timeout: float, cancel_event: asyncio.Event) -> None:
    """Stop a delegated turn that ran past its limit."""
    try:
        await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        return
    _log.warning("Delegated turn exceeded %ss — stopping it", int(timeout))
    cancel_event.set()


def _result(
    status: str,
    agent: str,
    *,
    agent_name: str = "",
    summary: str = "",
    message_id: str | None = None,
    tokens_used: int = 0,
    duration_ms: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the tool result handed back to the delegating agent.

    The key names are load-bearing: ``agent.py`` special-cases ``images`` and
    ``files`` as media attachments carrying pre-encoded payloads, so a
    delegation result must never use them.
    """
    out: dict[str, Any] = {
        "status": status,
        "agent": agent,
        "agent_name": agent_name or agent,
        "summary": summary,
        "tokens_used": tokens_used,
    }
    if duration_ms is not None:
        # Surfaced on the delegation tool card (ToolCallCard reads
        # output.duration_ms) — a result that never measured (early refusals)
        # omits it so the card skips the field instead of showing 0s.
        out["duration_ms"] = duration_ms
    if message_id:
        out["message_id"] = message_id
    if error:
        out["error"] = error
    return out


async def _emit(ctx: DelegationContext, event_type: str, data: dict) -> None:
    """Publish an event about the delegation on the TOP-LEVEL session.

    The child's own session is not registered and its transport drops anything
    unlisted, so delegation events travel on the parent's stream — where the
    parent turn's ``forward_events`` forwards them for free (an unknown event
    type simply matches nothing in its persistence branch).
    """
    session = getattr(ctx, "parent_session", None)
    if session is None:
        return
    try:
        await session.emit(event_type, **data)
    except Exception:
        _log.debug("Delegation event %s could not be emitted", event_type, exc_info=True)


def _display_name(definition_path: str) -> str:
    try:
        import frontmatter

        return str(frontmatter.load(definition_path).get("display_name") or "")
    except Exception:
        return ""
