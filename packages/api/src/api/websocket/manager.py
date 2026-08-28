"""WebSocket connection manager."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

from fastapi import WebSocket

_log = logging.getLogger("agents_universe.ws")


def _json_default(obj: object) -> object:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
        self._sessions: dict[str, Any] = {}  # ConversationSession keyed by conversation_id
        self._session_memories: dict[str, list[dict]] = {}  # Ephemeral session notes per conversation
        self._lock = asyncio.Lock()
        self._claimed_turns: set[str] = set()  # conversations with an in-flight turn claim
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
        no session is active.
        """
        async with self._lock:
            if ws is not None and self._connections.get(conversation_id) is not ws:
                return  # a newer socket has already replaced this one
            self._connections.pop(conversation_id, None)
            if conversation_id not in self._sessions:
                self._abort_events.pop(conversation_id, None)
                self._session_memories.pop(conversation_id, None)

    async def send(self, conversation_id: str, data: dict[str, Any]) -> bool:
        """Send a JSON message to the current WS for *conversation_id*.

        Returns ``True`` if delivered, ``False`` if no WS is connected or
        the send failed.  Failures cause the dead WS to be evicted so the
        next reconnect can register a fresh one.
        """
        async with self._lock:
            ws = self._connections.get(conversation_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(data, default=_json_default))
            return True
        except Exception as e:
            _log.warning("Failed to send to %s: %s", conversation_id, e)
            async with self._lock:
                if self._connections.get(conversation_id) is ws:
                    self._connections.pop(conversation_id, None)
            try:
                await ws.close(code=1011)
            except Exception:
                _log.debug("WebSocket close() failed during disconnect for %s", conversation_id)
            return False

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

    def signal_abort(self, conversation_id: str) -> None:
        event = self._abort_events.get(conversation_id)
        if event:
            event.set()

    def reset_abort(self, conversation_id: str) -> None:
        event = self._abort_events.get(conversation_id)
        if event:
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
