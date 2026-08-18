"""Conversation session — token tracking and event emission."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable


@dataclass
class SessionEvent:
    """An event emitted by the agent session for the WS handler to forward."""
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserInputEntry:
    """A user message queued for in-flight injection at the next step boundary.

    The API handler creates one per message received while the agent is
    running, pre-generating ``message_id`` (which doubles as the future DB
    row's primary key). The agent consumes it at a step boundary; the handler
    persists it via the ``user_message_injected`` event and resolves
    ``persisted`` so the agent can continue only once the message is durably
    stored.
    """

    message_id: str
    content: str
    attachments: list[dict]
    agent_id: str | None = None
    config_id: str | None = None
    attachment_records: list[dict] | None = None
    persisted: asyncio.Future[bool] | None = None
    consumed: bool = False


class ConversationSession:
    """Tracks state for one conversation turn (or agentic loop run)."""

    def __init__(
        self,
        conversation_id: str,
        project_id: str,
        user_id: str,
        token_budget: int = 128000,
        tokens_used: int = 0,
    ) -> None:
        self.conversation_id = conversation_id
        self.project_id = project_id
        self.user_id = user_id
        self.token_budget = token_budget
        self.tokens_used = tokens_used

        self._event_queue: asyncio.Queue[SessionEvent | None] = asyncio.Queue(maxsize=1000)
        self._closed = False
        self.abort_event = asyncio.Event()
        self._current_message_id: str = str(uuid.uuid4())
        self._pending_prompts: dict[str, asyncio.Future[str]] = {}
        self._user_input_queue: asyncio.Queue[UserInputEntry] = asyncio.Queue(maxsize=20)
        # message_id → entry, kept in sync with the queue for O(1) resolution
        self._pending_input_by_id: dict[str, UserInputEntry] = {}

        # Live streaming snapshot for WS reconnect sync.
        # Updated by forward_events; read by the WS handler when a new
        # client connects mid-stream.
        self.current_streaming_text: str = ""
        self.current_tool_calls: list[dict] = []

    def new_message(self) -> str:
        self._current_message_id = str(uuid.uuid4())
        return self._current_message_id

    @property
    def tokens_remaining(self) -> int:
        return self.token_budget - self.tokens_used

    @property
    def budget_exceeded(self) -> bool:
        """Whether accumulated usage has passed the display budget.

        This is observational only. The agent loop must not stop work because
        a conversation has consumed its configured budget.
        """
        return self.tokens_used > self.token_budget

    def add_tokens(self, count: int) -> bool:
        """Add tokens to the usage counter and return the budget observation."""
        if count > 0:
            self.tokens_used += count
        return not self.budget_exceeded

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> bool:
        """Add provider usage and return the budget observation."""
        total = prompt_tokens + completion_tokens
        if total > 0:
            self.tokens_used += total
        return not self.budget_exceeded

    async def emit(self, event_type: str, **data: Any) -> None:
        """Enqueue an event to be forwarded by the WS handler.

        Blocks until the consumer drains the queue. If the consumer stalls for
        more than 10 s the session is aborted so the agent doesn't run forever
        while no client is listening.
        """
        if self._closed:
            return
        try:
            await asyncio.wait_for(
                self._event_queue.put(SessionEvent(type=event_type, data=data)),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            import logging
            logging.getLogger("agent_core.session").warning(
                "Event queue blocked for 10 s on %s — consumer likely dead, aborting agent",
                event_type,
            )
            self.abort_event.set()
        except asyncio.CancelledError:
            # Re-raise so cancellation propagates immediately — swallowing it
            # (as the old combined except did) breaks structured cancellation:
            # the task would keep running until the next is_aborted() check.
            raise

    async def events(self) -> AsyncIterator[SessionEvent]:
        """Async generator that yields events until None sentinel is received."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        """Signal end of event stream. Safe to call multiple times."""
        if self._closed:
            return  # sentinel already in queue from first call
        self._closed = True
        # Use put_nowait for the sentinel — if the queue is full at this point
        # the consumer is stuck; force-clear the queue and push the sentinel so
        # forward_events() can exit cleanly. The stream content is kept: the
        # final stream_end tells the handler to persist the assistant message
        # and the stream_delta events carry its text — dropping either would
        # silently lose the whole reply from history. Terminal task events
        # (task_completed/task_failed/task_skipped) are kept too: they are the
        # only source of a task row's final status, and dropping them would
        # leave the row at pending/running for the handler's stale-task
        # reconcile to mark failed. user_message_injected is the ONLY writer of
        # an injected user message's DB row (the handler consumes the input and
        # waits for this event to persist it) — dropping it would erase the
        # user's words from history and the LLM context. image_output/
        # file_output are kept as well: the handler accumulates them into its
        # buffers and persists them at stream_end, so dropping them loses the
        # reply's attachments. Other event types (progress/tool updates) are
        # dropped; they are UI-only.
        if self._event_queue.full():
            kept: list[SessionEvent] = []
            while not self._event_queue.empty():
                try:
                    ev = self._event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if ev is not None and ev.type in (
                    "stream_delta",
                    "stream_end",
                    "task_completed",
                    "task_failed",
                    "task_skipped",
                    "user_message_injected",
                    "image_output",
                    "file_output",
                ):
                    kept.append(ev)
            # Leave one slot for the sentinel below so forward_events() can
            # still exit; the kept tail preserves the message text (newest
            # deltas last, stream_end at the very end).
            for ev in kept[-(self._event_queue.maxsize - 1):]:
                try:
                    self._event_queue.put_nowait(ev)
                except asyncio.QueueFull:
                    break
        try:
            self._event_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def request_user_selection(
        self,
        prompt_id: str,
        field_key: str,
        question: str,
        options: list[dict] | None = None,
        allow_other: bool = True,
        timeout: float = 300.0,
        *,
        kind: str = "selection",
        title: str | None = None,
        message: str | None = None,
        secret: bool = False,
        task_id: str | None = None,
        service_key: str | None = None,
        environment: str | None = None,
        save_to_project_secrets: bool = False,
        save_to_user_tokens: bool = False,
    ) -> str:
        """Emit a selection/input prompt event and wait for the user to respond.

        Blocks the agent until the UI sends back a user_selection_response message.

        Parameters
        ----------
        kind : "selection" | "text"
            "selection" shows radio/dropdown options (legacy default).
            "text" shows a free-text input field.
        secret : bool
            If True, the frontend shows a password input and the response is
            handled server-side: the plaintext never reaches the LLM. The Future
            resolves to an opaque confirmation string (e.g. "secret_saved"), not
            the actual secret value.
        save_to_project_secrets : bool
            When secret=True, instructs the WS handler to persist the value as a
            project-scoped secret before resolving the Future.
        save_to_user_tokens : bool
            When secret=True, instructs the WS handler to persist the value as a
            user-scoped token (user_tokens table) before resolving the Future.
            Mutually exclusive with save_to_project_secrets.
        service_key / environment : str | None
            Used together with save_to_project_secrets / save_to_user_tokens to
            identify the secret slot.
        task_id : str | None
            Associates this prompt with a running task for UI display.
        title / message : str | None
            Optional display overrides for the prompt dialog.

        Raises RuntimeError on timeout.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_prompts[prompt_id] = fut

        event_data: dict[str, Any] = {
            "prompt_id": prompt_id,
            "field_key": field_key,
            "question": question,
            "kind": kind,
            "allow_other": allow_other,
        }
        if options is not None:
            event_data["options"] = options
        if title is not None:
            event_data["title"] = title
        if message is not None:
            event_data["message"] = message
        if secret:
            event_data["secret"] = True
        if task_id is not None:
            event_data["task_id"] = task_id
        if service_key is not None:
            event_data["service_key"] = service_key
        if environment is not None:
            event_data["environment"] = environment
        if save_to_project_secrets:
            event_data["save_to_project_secrets"] = True
        if save_to_user_tokens:
            event_data["save_to_user_tokens"] = True

        await self.emit("user_selection_required", **event_data)
        # Also wake on abort: once the session is aborted the UI has closed
        # this prompt's path (emit can even fail with a full queue) and the
        # tool would otherwise block the full 300 s timeout on a prompt
        # nobody can ever answer.
        abort_waiter = asyncio.create_task(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                {asyncio.shield(fut), abort_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            abort_waiter.cancel()
            self._pending_prompts.pop(prompt_id, None)
        if not done:
            raise RuntimeError(
                f"User selection timed out after {timeout:.0f} s for field '{field_key}'"
            )
        if abort_waiter in done:
            raise RuntimeError("Session aborted while waiting for user input")
        return fut.result()

    def resolve_user_selection(self, prompt_id: str, value: str) -> bool:
        """Resolve a pending user_confirm Future. Returns True if found.

        For non-secret prompts, *value* is the user's chosen option or text.
        For secret prompts, the WS handler should call resolve_user_selection_secret()
        instead so that the plaintext never reaches the agent.
        """
        fut = self._pending_prompts.get(prompt_id)
        if fut is not None and not fut.done():
            fut.set_result(value)
            return True
        return False

    def resolve_user_selection_secret(self, prompt_id: str, *, saved: bool = True) -> bool:
        """Resolve a secret prompt without forwarding the plaintext to the agent.

        Called by the WS handler AFTER the secret has been encrypted and stored.
        The agent receives an opaque confirmation string, never the raw value.
        """
        fut = self._pending_prompts.get(prompt_id)
        if fut is not None and not fut.done():
            status = "secret_saved" if saved else "secret_save_failed"
            fut.set_result(status)
            return True
        return False

    def is_aborted(self) -> bool:
        return self.abort_event.is_set()

    def abort(self) -> None:
        self.abort_event.set()

    # --- In-flight user input injection ------------------------------------

    def enqueue_user_input(self, entry: UserInputEntry) -> bool:
        """Queue a user message for the agent to consume at its next step
        boundary. Returns False when the queue is full."""
        try:
            self._user_input_queue.put_nowait(entry)
            self._pending_input_by_id[entry.message_id] = entry
            return True
        except asyncio.QueueFull:
            return False

    def dequeue_user_input(self) -> UserInputEntry | None:
        """Pop the next pending user message without blocking — the agent
        polls at step boundaries, never blocks the main loop on this.

        The entry stays registered by message_id so the handler can still
        resolve its persistence Future after the agent consumed it (the
        user_message_injected event is processed asynchronously).
        """
        try:
            return self._user_input_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def has_pending_user_input(self) -> bool:
        return not self._user_input_queue.empty()

    def resolve_input_persisted(
        self,
        message_id: str,
        ok: bool,
        attachment_records: list[dict] | None = None,
    ) -> None:
        """Resolve the persistence Future for an injected message.

        Called by the WS handler after the message was written to the DB (or
        rejected). ``attachment_records`` carries the prepared attachment
        records (inline_text/image_data) the agent needs to build the LLM
        user content.
        """
        entry = self._pending_input_by_id.pop(message_id, None)
        if entry is not None:
            if attachment_records is not None:
                entry.attachment_records = attachment_records
            if entry.persisted is not None and not entry.persisted.done():
                entry.persisted.set_result(ok)

