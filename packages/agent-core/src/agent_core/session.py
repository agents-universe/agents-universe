"""Conversation session — token tracking and event emission."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

# How long emit() waits for the consumer before declaring it dead and
# aborting the session. Constant so tests can shrink it.
_EMIT_PUT_TIMEOUT_S = 10.0


@dataclass
class SessionEvent:
    """An event emitted by the agent session for the WS handler to forward."""
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptOutcome:
    """How one interactive prompt ended, keyed by its caller-supplied signature.

    ``status`` is ``"answered"`` | ``"timed_out"`` | ``"dismissed"``.
    """
    status: str
    value: str | None = None


class UserSelectionTimeoutError(RuntimeError):
    """A prompt expired unanswered.

    Subclasses RuntimeError so safety gates that catch RuntimeError around
    request_user_selection keep their behavior unchanged.
    """


class UserSelectionAbortedError(RuntimeError):
    """The run was aborted while a prompt was pending."""


class UserSelectionUnavailableError(RuntimeError):
    """The run is non-interactive, so nobody could ever answer a prompt.

    Subclasses RuntimeError for the same reason as UserSelectionTimeoutError:
    every safety gate that catches RuntimeError around request_user_selection
    degrades to its deny/error path unchanged.
    """


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
        prompt_sink: "ConversationSession | None" = None,
        interactive: bool = True,
    ) -> None:
        self.conversation_id = conversation_id
        self.project_id = project_id
        self.user_id = user_id
        self.token_budget = token_budget
        self.tokens_used = tokens_used
        # Occupancy of the LATEST provider request (prompt+completion of the
        # last usage chunk) — what the context meter displays. Distinct from
        # the lifetime billing ledger `tokens_used`.
        self.context_tokens: int = 0
        # Window of the model that last reported usage (None until first call).
        self.context_window: int | None = None

        self._event_queue: asyncio.Queue[SessionEvent | None] = asyncio.Queue(maxsize=1000)
        self._closed = False
        self.abort_event = asyncio.Event()
        # First-cause token for the abort ("event_queue_blocked", "abort",
        # ...) so downstream settlement can record WHY the run stopped
        # instead of guessing Stop-vs-crash. First abort() wins.
        self.abort_reason: str | None = None
        self._current_message_id: str = str(uuid.uuid4())
        self._pending_prompts: dict[str, asyncio.Future[str]] = {}
        # The emitted payload of each in-flight prompt, keyed by prompt_id.
        # A prompt exists only here and in the client's memory — it is never
        # part of the message history — so the transport layer replays these
        # when a client reconnects (conversation switch, page reload).
        self._pending_prompt_events: dict[str, dict[str, Any]] = {}
        # A delegated (nested) turn runs its own session — it needs its own
        # event queue — but a user's answer is routed by the WS handler through
        # the conversation's *registered* session. Prompts therefore register
        # on the sink (the top-level session) and record their owning session,
        # so answering one also lifts that session's prompt pause.
        # Whether a client is attached that can answer prompts. False for
        # headless runs (scheduled tasks, published agents): the session is
        # still built and registered, but no user_selection_response can
        # ever arrive.
        self.interactive = interactive
        self._prompt_sink: ConversationSession | None = prompt_sink
        self._prompt_owners: dict[str, ConversationSession] = {}
        # Interactive-prompt ledger, keyed by a caller-supplied signature
        # (field key + question). Tool-level callers use it to hand back a
        # repeated question's recorded outcome instead of showing the dialog
        # a second time, and to remember a prompt that already failed.
        self._prompt_outcomes: dict[tuple[str, str], PromptOutcome] = {}
        self._prompt_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._prompts_paused = False
        self._user_input_queue: asyncio.Queue[UserInputEntry] = asyncio.Queue(maxsize=20)
        # message_id → entry, kept in sync with the queue for O(1) resolution
        self._pending_input_by_id: dict[str, UserInputEntry] = {}

        # Live streaming snapshot for WS reconnect sync.
        # Updated by forward_events; read by the WS handler when a new
        # client connects mid-stream.
        self.current_streaming_text: str = ""
        # Thinking accumulated this turn (same lifecycle as the text buffer).
        self.current_streaming_thinking: str = ""
        # Coarse turn phase for the status line / reconnect sync
        # ("waiting_model" | "thinking" | "responding" | "running_tool" |
        # "compressing" | "degrading"). Updated by the API's forward_events.
        self.current_turn_phase: str = "waiting_model"
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
        longer than ``_EMIT_PUT_TIMEOUT_S`` the session is aborted so the
        agent doesn't run forever while no client is listening.
        """
        if self._closed:
            return
        try:
            await asyncio.wait_for(
                self._event_queue.put(SessionEvent(type=event_type, data=data)),
                timeout=_EMIT_PUT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            import logging
            logging.getLogger("agent_core.session").warning(
                "Event queue blocked for %s s on %s (conversation=%s, queued=%d) — consumer likely dead, aborting agent",
                _EMIT_PUT_TIMEOUT_S, event_type, self.conversation_id,
                self._event_queue.qsize(),
            )
            self.abort("event_queue_blocked")
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
        # reply's attachments. user_selection_cancelled is kept too: a prompt
        # that timed out / was aborted must still reach the client so it can
        # dismiss the dialog — dropping it would leave a zombie prompt pinned
        # in the UI forever. thinking_delta is kept too: the handler
        # accumulates it into the assistant row's persisted `thinking`
        # column, so dropping it would silently truncate stored reasoning.
        # Other event types (progress/tool updates) are dropped; they are
        # UI-only.
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
                    "user_selection_cancelled",
                    "thinking_delta",
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

    # --- Interactive-prompt ledger -----------------------------------------
    #
    # Prompt state lives for one turn (a session object is created per turn),
    # so the ledger suppresses repeats within a turn. Letting an answer outlive
    # the turn is the caller's job — persist it as a project setting.

    def get_prompt_outcome(self, signature: tuple[str, str] | None) -> PromptOutcome | None:
        """Return the recorded outcome for a prompt signature, if any."""
        if not signature:
            return None
        return self._prompt_outcomes.get(signature)

    def record_prompt_outcome(
        self,
        signature: tuple[str, str] | None,
        status: str,
        value: str | None = None,
    ) -> None:
        """Record how an interactive prompt ended.

        An answered entry is never overwritten by a failure: two callers can
        ask the same question concurrently, and the duplicate that times out
        must not erase the answer the user actually gave.
        """
        if not signature:
            return
        existing = self._prompt_outcomes.get(signature)
        if existing is not None and existing.status == "answered" and status != "answered":
            return
        self._prompt_outcomes[signature] = PromptOutcome(status=status, value=value)

    def prompt_lock(self, signature: tuple[str, str]) -> asyncio.Lock:
        """Lock serializing callers that ask the same question concurrently."""
        lock = self._prompt_locks.get(signature)
        if lock is None:
            lock = asyncio.Lock()
            self._prompt_locks[signature] = lock
        return lock

    def note_prompt_timeout(self) -> None:
        """A prompt expired unanswered — stop prompting for the rest of the turn."""
        self._prompts_paused = True

    @property
    def interactive_prompts_paused(self) -> bool:
        """Whether an unanswered prompt paused further interactive prompts."""
        return self._prompts_paused

    def _note_user_present(self) -> None:
        """Record that the user is present again (answered a prompt, or sent a
        message mid-run): lift the timeout pause and let questions that failed
        be asked once more. Answers already given stay recorded — re-asking
        those is exactly the repeat this ledger exists to stop."""
        self._prompts_paused = False
        self._prompt_outcomes = {
            sig: outcome
            for sig, outcome in self._prompt_outcomes.items()
            if outcome.status == "answered"
        }

    def _prompt_store(self) -> "ConversationSession":
        """The session that owns prompt bookkeeping: this one, or the sink.

        A delegated turn's session is deliberately not registered with the
        connection manager (the top-level turn owns that registration), and the
        WS handler resolves an answer through the conversation's registered
        session. Registering prompts on the sink is what makes a question asked
        by a delegated agent answerable at all.
        """
        store = self
        while store._prompt_sink is not None:
            store = store._prompt_sink
        return store

    def _note_owner_present(self, prompt_id: str) -> None:
        """Lift the pause on whoever asked, when this session holds the prompt."""
        owner = self._prompt_owners.get(prompt_id)
        if owner is not None and owner is not self:
            owner._note_user_present()

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

        Raises UserSelectionTimeoutError on timeout,
        UserSelectionAbortedError when the run is aborted, and
        UserSelectionUnavailableError when the run is non-interactive;
        all subclass RuntimeError.
        """
        store = self._prompt_store()
        # Headless runs (scheduled tasks, published agents) build and register
        # a session, but no client is attached — nobody can ever answer, so
        # waiting out the 120–300 s timeout only stalls the turn. The
        # ANSWERER's interactivity decides: prompts register on the sink
        # store, so a delegated turn under a headless parent is refused too,
        # while an interactive parent still answers its delegates' prompts.
        if not store.interactive:
            raise UserSelectionUnavailableError(
                "interactive prompts are disabled in this run — the agent is "
                "operating unattended; do not ask the user for input, pick "
                "the safe default and continue"
            )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        store._pending_prompts[prompt_id] = fut
        if store is not self:
            store._prompt_owners[prompt_id] = self

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

        # Register the payload BEFORE emitting: a client connecting in the
        # window between the emit and this line would get a sync event that
        # replays nothing, and the dialog would be lost until the next prompt.
        store._pending_prompt_events[prompt_id] = event_data
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
        except asyncio.CancelledError:
            # Torn down mid-question (a delegated turn hitting its timeout, or
            # a Stop that cancelled the run outright). Neither the timeout nor
            # the abort arm below runs on this path, so without this notice the
            # client keeps a dialog nobody can ever answer.
            try:
                await self.emit(
                    "user_selection_cancelled",
                    prompt_id=prompt_id,
                    field_key=field_key,
                    reason="cancelled",
                )
            except BaseException:
                # Best effort — the notice must never mask the cancellation.
                pass
            raise
        finally:
            abort_waiter.cancel()
            store._pending_prompts.pop(prompt_id, None)
            store._pending_prompt_events.pop(prompt_id, None)
            store._prompt_owners.pop(prompt_id, None)
        if not done:
            # The client must dismiss the dialog it is still showing for this
            # prompt — otherwise the UI keeps a zombie prompt that never
            # resolves and every later user_confirm stacks another dialog on
            # top (the "超时后再对话弹窗不出现/错乱" symptom).
            await self.emit(
                "user_selection_cancelled",
                prompt_id=prompt_id,
                field_key=field_key,
                reason="timeout",
            )
            raise UserSelectionTimeoutError(
                f"User selection timed out after {timeout:.0f} s for field '{field_key}'"
            )
        if abort_waiter in done:
            await self.emit(
                "user_selection_cancelled",
                prompt_id=prompt_id,
                field_key=field_key,
                reason="aborted",
            )
            raise UserSelectionAbortedError("Session aborted while waiting for user input")
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
            self._note_user_present()
            self._note_owner_present(prompt_id)
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
            self._note_user_present()
            self._note_owner_present(prompt_id)
            return True
        return False

    def pending_prompt_events(self) -> list[dict[str, Any]]:
        """Snapshot of the prompts still awaiting user input.

        A client that (re)connects to the conversation gets these replayed so
        it can restore the dialog: the prompt's Future lives only in this
        session, and the message history holds no trace of it, so a client
        that dropped the dialog (switched conversations and came back, or
        reloaded the page) would otherwise show a conversation waiting on an
        answer it cannot give.
        """
        return [dict(payload) for payload in self._pending_prompt_events.values()]

    def is_aborted(self) -> bool:
        return self.abort_event.is_set()

    def abort(self, reason: str = "abort") -> None:
        """Set the abort event, recording the first cause."""
        if self.abort_reason is None:
            self.abort_reason = reason
        self.abort_event.set()

    # --- In-flight user input injection ------------------------------------

    def enqueue_user_input(self, entry: UserInputEntry) -> bool:
        """Queue a user message for the agent to consume at its next step
        boundary. Returns False when the queue is full."""
        try:
            self._user_input_queue.put_nowait(entry)
            self._pending_input_by_id[entry.message_id] = entry
            # The user is back at the keyboard: unanswered prompts may be asked
            # again (answers already given stay recorded).
            self._note_user_present()
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
