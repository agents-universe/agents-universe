"""Abstract LLM provider interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator


class StopReason(str, Enum):
    """Normalized stop reasons across all providers.

    Anthropic: end_turn, tool_use, max_tokens, pause_turn, refusal
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
            "tool_use": cls.TOOL_USE,
            "max_tokens": cls.MAX_TOKENS,
            "pause_turn": cls.PAUSE_TURN,
            "refusal": cls.REFUSAL,
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


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | list  # str for text; list for multimodal (vision)
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # for tool role messages


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
