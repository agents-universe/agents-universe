"""WebSocket connection manager."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from api.json_utils import json_default as _json_default

_log = logging.getLogger("agents_universe.ws")

# Bound on a single WS frame send. Without it a half-open TCP connection
# (client vanished behind the proxy without RST) blocks forward_events
# indefinitely: the consumer stalls, the session event queue fills, and
# session.emit's backpressure timeout kills the run. Must stay below
# agent_core.session's emit put-timeout so the consumer always unblocks
# before the producer gives up. Tests shrink it.
_SEND_TIMEOUT_S = 5.0


class ConnectionManager:
    """Manages active WebSocket connections keyed by conversation_id.

    A conversation may have at most one *active* WS at a time.  When a new
    socket connects it replaces the previous one — but the associated
    **session**, **abort event**, and **session memories** are *not* removed
    on disconnect.  This allows the agent to continue running in the
    background after the user switches conversations; the new WS picks up
    live events via ``manager.send()`` when the user returns.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._abort_events: dict[str, asyncio.Event] = {}
        # First-cause token for each set abort event ("ws_abort_frame",
        # "publish_sse_client_disconnect", ...). Kept alongside the event and
        # popped with it so interrupted runs can say WHY in logs and
        # conversation_runs.error_message instead of guessing Stop-vs-crash.
        self._abort_reasons: dict[str, str] = {}
        self._sessions: dict[str, Any] = {}  # ConversationSession keyed by conversation_id
        self._session_memories: dict[str, list[dict]] = {}  # Ephemeral session notes per conversation
        self._lock = asyncio.Lock()
        self._claimed_turns: set[str] = set()  # conversations with an in-flight turn claim
        # Serializes delegated (nested) turns per conversation — see
        # delegation_gate().
        self._delegation_gates: dict[str, asyncio.Lock] = {}
        self._turn_guard = asyncio.Lock()
        # Messages received while a turn is claimed but before the session is
        # registered (the history-load window) — drained into the session by
        # _handle_message right after register_session.
        self._pending_injections: dict[str, list[dict]] = {}

    async def claim_turn(self, conversation_id: str) -> bool:
        """Atomically claim a conversation for a new agent turn.

        register_session() only happens partway through _handle_message
        (after history load), so two WebSocket connections sending within
        that window would both pass is_session_active() and run concurrent
        turns. The claim is taken when the message frame is received and
        released when the turn ends (see release_turn).
        """
        async with self._turn_guard:
            if conversation_id in self._claimed_turns:
                return False
            self._claimed_turns.add(conversation_id)
            return True

    def release_turn(self, conversation_id: str) -> None:
        """Release a turn claim. Idempotent — safe on any turn end path."""
        self._claimed_turns.discard(conversation_id)
        # Only top-level turns release the claim, so no delegation can still be
        # in flight here — dropping the gate keeps a finished conversation from
        # leaking one lock per delegation.
        self._delegation_gates.pop(conversation_id, None)

    def is_turn_active(self, conversation_id: str) -> bool:
        """True while an agent turn runs on this conversation.

        The turn claim is taken when the WS frame arrives, seconds before the
        session is registered — REST endpoints (delete/compress) checking only
        is_session_active() would see the conversation as idle in that window
        and mutate it while the agent keeps writing into it.
        """
        return (
            conversation_id in self._claimed_turns
            or self.is_session_active(conversation_id)
        )


    async def connect(self, conversation_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections[conversation_id] = ws
            # Preserve existing abort event — a background agent's
            # _watch_abort coroutine is waiting on it.
            if conversation_id not in self._abort_events:
                self._abort_events[conversation_id] = asyncio.Event()

    async def disconnect(self, conversation_id: str, ws: WebSocket | None = None) -> None:
        """Remove the WS connection for *conversation_id*.

        Session, abort event and session memories are **kept** when an agent
        is still running, so that a reconnected WS can resume receiving
        events and the user can still abort.  They are cleaned up only when
        no turn is in flight.
        """
        async with self._lock:
            if ws is not None and self._connections.get(conversation_id) is not ws:
                return  # a newer socket has already replaced this one
            self._connections.pop(conversation_id, None)
            # A claimed turn has not registered its session yet (history-load
            # window), but it IS running: run_turn wires its abort watcher to
            # this very event. Popping it here would leave Stop setting a
            # fresh event nobody waits on.
            if (
                conversation_id not in self._sessions
                and conversation_id not in self._claimed_turns
            ):
                self._abort_events.pop(conversation_id, None)
                self._abort_reasons.pop(conversation_id, None)
                self._session_memories.pop(conversation_id, None)

    async def send(self, conversation_id: str, data: dict[str, Any]) -> bool:
        """Send a JSON message to the current WS for *conversation_id*.

        Returns ``True`` if delivered, ``False`` if no WS is connected or
        the send failed.  Failures and send timeouts cause the dead WS to be
        evicted so the next reconnect can register a fresh one.  A timeout is
        treated like "no WS connected": the turn keeps running and persisting
        to the DB — a half-open socket must not abort the run.
        """
        async with self._lock:
            ws = self._connections.get(conversation_id)
        if not ws:
            return False
        try:
            await asyncio.wait_for(
                ws.send_text(json.dumps(data, default=_json_default)),
                timeout=_SEND_TIMEOUT_S,
            )
            return True
        except asyncio.TimeoutError:
            _log.warning(
                "WS send timed out after %.1fs for %s — evicting half-open connection",
                _SEND_TIMEOUT_S, conversation_id,
            )
            await self._evict(conversation_id, ws)
            return False
        except Exception as e:
            _log.warning("Failed to send to %s: %s", conversation_id, e)
            await self._evict(conversation_id, ws)
            return False

    async def _evict(self, conversation_id: str, ws: WebSocket) -> None:
        """Drop *ws* if it is still the current connection and close it."""
        async with self._lock:
            if self._connections.get(conversation_id) is ws:
                self._connections.pop(conversation_id, None)
        try:
            await ws.close(code=1011)
        except Exception:
            _log.debug("WebSocket close() failed during eviction for %s", conversation_id)

    def register_session(self, conversation_id: str, session: Any) -> None:
        self._sessions[conversation_id] = session

    def get_session(self, conversation_id: str) -> Any | None:
        return self._sessions.get(conversation_id)

    def deregister_session(self, conversation_id: str) -> None:
        """Remove the session after the agent finishes.

        Abort events and session memories are cleaned up only when no WS
        is connected (user has navigated away entirely).
        """
        self._sessions.pop(conversation_id, None)
        if conversation_id not in self._connections:
            self._abort_events.pop(conversation_id, None)
            self._abort_reasons.pop(conversation_id, None)
            self._session_memories.pop(conversation_id, None)

    def is_session_active(self, conversation_id: str) -> bool:
        """Whether an agent session is currently running for *conversation_id*."""
        return conversation_id in self._sessions

    def get_running_conversations(self, user_id: str) -> set[str]:
        """Conversation IDs with active sessions for *user_id*."""
        result: set[str] = set()
        for conv_id, sess in self._sessions.items():
            uid = getattr(sess, "user_id", None)
            if uid == user_id:
                result.add(conv_id)
        return result

    def get_running_for_project(self, project_id: str) -> set[str]:
        """Conversation IDs with active sessions whose project is *project_id*.

        Used by project deletion: an in-flight agent session holds the
        workspace open (tool calls, media dir, work dir); deleting underneath
        it would make every subsequent tool call fail mid-turn.
        """
        result: set[str] = set()
        for conv_id, sess in self._sessions.items():
            if getattr(sess, "project_id", None) == project_id:
                result.add(conv_id)
        return result

    def get_abort_event(self, conversation_id: str) -> asyncio.Event | None:
        return self._abort_events.get(conversation_id)

    def delegation_gate(self, conversation_id: str) -> asyncio.Lock:
        """Return the conversation's delegation lock, creating it if absent.

        A single turn can fan out (``plan_task`` runs up to three tasks in
        parallel, and each may delegate), and two nested turns in one
        conversation would interleave their child messages and race for the
        same message sequence number. Serializing them keeps the transcript
        deterministic.

        Only the outermost delegation of a chain acquires this: a descendant
        inherits the already-held gate instead of re-entering it, which would
        deadlock (the ancestor holds it for the whole of its child's turn).
        """
        gate = self._delegation_gates.get(conversation_id)
        if gate is None:
            gate = asyncio.Lock()
            self._delegation_gates[conversation_id] = gate
        return gate

    def ensure_abort_event(self, conversation_id: str) -> asyncio.Event:
        """Return the conversation's abort event, creating it if absent.

        WS turns get theirs from ``connect()``; SSE publish streams never open
        a socket, so without this their abort would be a no-op (signal_abort
        on a missing event is silently dropped). Idempotent and thread-safe by
        construction (single-threaded event loop).
        """
        event = self._abort_events.get(conversation_id)
        if event is None:
            event = asyncio.Event()
            self._abort_events[conversation_id] = event
        return event

    def signal_abort(self, conversation_id: str, *, reason: str = "abort_signal") -> None:
        """Set the conversation's abort event, recording the first cause.

        ``reason`` is a stable token ("ws_abort_frame",
        "publish_sse_client_disconnect", ...) — first cause wins so a
        follow-up signal cannot overwrite the original attribution.
        """
        event = self._abort_events.get(conversation_id)
        if event:
            self._abort_reasons.setdefault(conversation_id, reason)
            event.set()

    def get_abort_reason(self, conversation_id: str) -> str | None:
        return self._abort_reasons.get(conversation_id)

    def reset_abort(self, conversation_id: str) -> None:
        event = self._abort_events.get(conversation_id)
        if event:
            self._abort_reasons.pop(conversation_id, None)
            event.clear()

    # --- In-flight injection buffering (claim window) ---

    # Upper bound mirrors the session user-input queue (maxsize=20): the claim
    # window is transient, but an unbounded buffer would let a fast sender
    # grow process memory without limit (every other input path is capped).
    _PENDING_INJECTION_LIMIT = 20

    def enqueue_pending_injection(self, conversation_id: str, msg: dict) -> bool:
        """Buffer a message received while the turn is claimed but its
        session is not registered yet. Drained into the session by
        _handle_message after register_session, so the agent consumes it at
        its first step boundary. Returns False when the buffer is full —
        the caller rejects the message with input_rejected."""
        buf = self._pending_injections.setdefault(conversation_id, [])
        if len(buf) >= self._PENDING_INJECTION_LIMIT:
            return False
        buf.append(msg)
        return True

    def drain_pending_injections(self, conversation_id: str) -> list[dict]:
        """Return and clear the buffered messages for *conversation_id*."""
        return self._pending_injections.pop(conversation_id, [])

    def has_pending_injections(self, conversation_id: str) -> bool:
        return bool(self._pending_injections.get(conversation_id))

    def discard_pending_injections(self, conversation_id: str) -> None:
        """Drop buffered messages whose turn died before the session was
        registered. The next turn's drain self-heals (the UI already showed
        input_queued, and the user can resend — the message never reached
        the agent or the DB)."""
        self._pending_injections.pop(conversation_id, None)

    # --- Session memory (ephemeral, in-memory only) ---

    def get_session_memories(self, conversation_id: str) -> list[dict]:
        # setdefault (not get): the returned list IS the manager's backing
        # store — agent-core memory_rw appends to it in place, so a
        # `get()`-returned orphan list would never persist to the manager
        # and every recall/GET /memories/session across turns came back
        # empty.
        return self._session_memories.setdefault(conversation_id, [])

    def add_session_memory(self, conversation_id: str, note: str, timestamp: float) -> None:
        notes = self._session_memories.setdefault(conversation_id, [])
        if len(notes) >= 20:
            notes.pop(0)
        notes.append({"note": note, "timestamp": timestamp})


manager = ConnectionManager()
