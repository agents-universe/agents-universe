"""Abstract LLM provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator


class StopReason(str, Enum):
    """Normalized stop reasons across all providers.

    Anthropic: end_turn, tool_use, max_tokens, pause_turn, refusal,
    stop_sequence, model_context_window_exceeded
    OpenAI: stop, tool_calls, length, content_filter
    """
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    CONTEXT_EXCEEDED = "context_exceeded"
    UNKNOWN = "unknown"

    @classmethod
    def from_anthropic(cls, raw: str | None) -> "StopReason":
        if not raw:
            return cls.UNKNOWN
        mapping = {
            "end_turn": cls.END_TURN,
            "stop_sequence": cls.END_TURN,
            "tool_use": cls.TOOL_USE,
            "max_tokens": cls.MAX_TOKENS,
            "pause_turn": cls.PAUSE_TURN,
            "refusal": cls.REFUSAL,
            # Anthropic's stop reason when the REQUEST exceeds the model's
            # context window. Unmapped it became UNKNOWN, which the agent
            # treats as a normal end_turn — the context_exceeded event (and
            # the UI's compress-and-retry affordance) never fired.
            "model_context_window_exceeded": cls.CONTEXT_EXCEEDED,
        }
        return mapping.get(raw, cls.UNKNOWN)

    @classmethod
    def from_openai(cls, raw: str | None) -> "StopReason":
        if not raw:
            return cls.UNKNOWN
        mapping = {
            "stop": cls.END_TURN,
            "tool_calls": cls.TOOL_USE,
            "length": cls.MAX_TOKENS,
            "content_filter": cls.CONTENT_FILTER,
        }
        return mapping.get(raw, cls.UNKNOWN)


def thinking_enabled_by_env() -> bool:
    """Shared extended-thinking switch (AGENT_EXTENDED_THINKING, default on).

    Providers that must REQUEST thinking (Anthropic, Gemini) consult this;
    providers that only PARSE an existing reasoning channel ignore it.
    """
    import os
    return os.environ.get("AGENT_EXTENDED_THINKING", "1").strip().lower() not in ("0", "false", "no")


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list  # str for text; list for multimodal (vision)
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # for tool role messages
    # Anthropic extended-thinking blocks for same-turn tool-loop replay only
    # (the API requires signed thinking blocks back on the next request when
    # the assistant turn carries tool_use). Never persisted to the DB —
    # history compression would invalidate the signatures.
    thinking_blocks: list[dict] | None = None


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict  # JSON Schema object


@dataclass
class StreamChunk:
    delta: str = ""
    tool_call_delta: dict | None = None
    finish_reason: str | None = None  # raw provider string
    stop_reason: StopReason | None = None  # normalized
    usage: dict | None = None  # {"prompt_tokens": int, "completion_tokens": int}
    # Extended thinking / reasoning output (mutually exclusive with delta in
    # practice: a provider yields either visible text or thinking per event).
    thinking: str = ""
    # Anthropic signature closing a thinking block — required to replay the
    # block back on the next tool-loop request.
    thinking_signature: str | None = None


@dataclass
class CompletionResult:
    message: Message
    usage: dict  # {"prompt_tokens": int, "completion_tokens": int}
    model: str
    finish_reason: str  # raw provider string
    stop_reason: StopReason = StopReason.UNKNOWN


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        """Non-streaming completion."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming completion. Yields StreamChunk objects."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text. Optional — not used by framework."""
        raise NotImplementedError(f"{type(self).__name__} does not support embeddings")

    @property
    @abstractmethod
    def context_window(self) -> int:
        """Maximum context tokens this provider/model supports."""
        ...

    @property
    @abstractmethod
    def supports_tool_calls(self) -> bool:
        """Whether this provider supports tool/function calling."""
        ...

    @property
    @abstractmethod
    def supports_vision(self) -> bool:
        """Whether this provider supports image inputs."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The model identifier string."""
        ...

    async def close(self) -> None:
        """Release any held resources (HTTP clients, connections). Safe to call multiple times."""

    def secret_values(self) -> list[str]:
        """Credential values this provider puts on the wire.

        Every concrete provider stores its key as ``_api_key``. Provider
        exceptions (SDK auth errors, h11's header rejection) can embed the
        key, so callers that log exception text run it through :meth:`scrub`
        first — tokens are never logged (CLAUDE.md token-security rule).
        """
        key = getattr(self, "_api_key", None)
        return [key] if isinstance(key, str) and key else []

    def scrub(self, text: str) -> str:
        """Replace every :meth:`secret_values` entry in *text* with ``[REDACTED]``.

        Matches the raw value and the bytes-repr escaped form h11/tracebacks
        quote — see :func:`agent_core.tools._http.redact_secret`.
        """
        from ..tools._http import redact_secret
        for value in self.secret_values():
            text = redact_secret(text, value)
        return text
