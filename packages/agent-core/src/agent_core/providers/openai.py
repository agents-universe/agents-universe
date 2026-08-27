"""OpenAI provider (also used for Azure OpenAI)."""
from __future__ import annotations

import json
import re
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
)
from ..compressor import MAX_OUTPUT_RESERVE

def _context_window(model: str) -> int:
    m = model.lower()
    if m.startswith("gpt-5"):
        return 1_000_000
    if any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")):
        return 200_000
    # GLM-5.3 is the flagship (1M context); earlier GLM lines get the fallback.
    glm_ver = re.match(r"^glm[-_]?(\d+(?:\.\d+)*)", m)
    if glm_ver and float(glm_ver.group(1)) >= 5.3:
        return 1_000_000
    if "gemini-2.5" in m or "gemini-3" in m:
        return 1_000_000
    if "gemini" in m:
        return 1_000_000
    # gpt-4o, gpt-4-turbo, gpt-4o-mini, etc.
    return 128_000


def _http_timeout() -> httpx.Timeout:
    """Phased request timeout for provider HTTP clients.

    A 60s scalar applied to every phase made READ 60s: a large-prompt
    stream (100k+ token histories) routinely needs over a minute for the
    first chunk, so turns died with "Request timed out" after the SDK's
    retries. The openai SDK adopts a custom http_client's timeout as its
    per-request timeout (overriding its own 600s default), so the phases
    must be right here: read generous, connect tight, write sized for
    multi-MB uploads.
    """
    return httpx.Timeout(300.0, connect=10.0, write=120.0, pool=60.0)


class OpenAIProvider(LLMProvider):
    """OpenAI via the openai SDK."""

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str | None = None, ssl_verify: bool = False, url_mode: str = "base_url", context_window: int | None = None) -> None:
        kwargs: dict = {"api_key": api_key}
        if base_url:
            b = base_url.rstrip("/")
            if url_mode == "full_url":
                # User provided the complete endpoint URL.
                # Strip /chat/completions suffix if present (SDK re-appends it).
                if b.endswith("/chat/completions"):
                    b = b[: -len("/chat/completions")]
                kwargs["base_url"] = b
            else:
                # Base URL mode: auto-append /v1 if no version segment detected.
                kwargs["base_url"] = b if re.search(r"/v\d+$", b) else f"{b}/v1"
        kwargs["http_client"] = httpx.AsyncClient(verify=ssl_verify, timeout=_http_timeout())
        # One retry still covers transient 429/5xx; the SDK default (2) would
        # stretch a dead endpoint to 3 x 300s reads before surfacing.
        kwargs["max_retries"] = 1
        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        # Per-config override from Settings → AI Models; None = name-matched default.
        self._context_window_override = context_window

    async def close(self) -> None:
        """Close the SDK's httpx client (connection pool).

        Agent.close() iterates _provider_cache and calls provider.close() on
        every provider; without this override each message leaks a keep-alive
        connection pool for the process lifetime.
        """
        try:
            await self._client.close()
        except Exception:
            pass

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def context_window(self) -> int:
        return self._context_window_override or _context_window(self._model)

    @property
    def supports_tool_calls(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        _NO_VISION = ("gpt-3.5",)
        return not any(self._model.startswith(p) for p in _NO_VISION)

    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        out = []
        for msg in messages:
            if msg.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
                })
            elif isinstance(msg.content, list):
                content = []
                for part in msg.content:
                    if part.get("type") == "image":
                        content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{part['media_type']};base64,{part['data']}"
                            },
                        })
                    else:
                        content.append({"type": "text", "text": part.get("text", "")})
                d: dict = {"role": msg.role, "content": content}
                if msg.tool_calls:
                    d["tool_calls"] = msg.tool_calls
                out.append(d)
            else:
                d: dict = {"role": msg.role, "content": msg.content}
                if msg.tool_calls:
                    d["tool_calls"] = msg.tool_calls
                out.append(d)
        return out

    def _to_openai_tools(self, tools: list[ToolDefinition]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def _is_reasoning_model(self) -> bool:
        """o-series / gpt-5 reject max_tokens and temperature (HTTP 400).

        Mirrors the AzureOpenAIProvider override; kept on the base class so
        plain "openai"-type configs of reasoning models work too.
        """
        m = self._model.lower()
        return any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")) or "gpt-5" in m

    def _clamp_max_tokens(self, max_tokens: int) -> int:
        """Cap output tokens at the non-reasoning ceiling.

        Agent configs default max_tokens to 128000; gpt-4o-class models cap
        output at 16384 and reject the raw value with a 400. Reasoning
        models accept large max_completion_tokens and are left untouched.
        """
        if self._is_reasoning_model():
            return max_tokens
        return min(max_tokens, MAX_OUTPUT_RESERVE)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = Message(
            role="assistant",
            content=choice.message.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (choice.message.tool_calls or [])
            ] or None,
        )
        raw_stop = choice.finish_reason
        # Some OpenAI-compatible gateways (vLLM/LM Studio/Ollama) omit usage —
        # guard against AttributeError or compression silently fails forever
        # on that gateway .
        usage = response.usage
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            } if usage is not None else {},
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_openai(raw_stop),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice:
                    delta = choice.delta
                    text = delta.content or ""
                    finish = choice.finish_reason
                    normalized = StopReason.from_openai(finish) if finish else None

                    if delta.tool_calls:
                        if text:
                            yield StreamChunk(delta=text, tool_call_delta=None, finish_reason=None)
                        for tc in delta.tool_calls:
                            tc_delta = {
                                "index": tc.index,
                                "id": tc.id or "",
                                "function": {
                                    "name": getattr(tc.function, "name", "") or "",
                                    "arguments": getattr(tc.function, "arguments", "") or "",
                                },
                            }
                            yield StreamChunk(
                                delta="",
                                tool_call_delta=tc_delta,
                                finish_reason=finish,
                                stop_reason=normalized,
                            )
                    else:
                        yield StreamChunk(
                            delta=text,
                            tool_call_delta=None,
                            finish_reason=finish,
                            stop_reason=normalized,
                        )
                elif chunk.usage:
                    yield StreamChunk(
                        usage={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        },
                    )
        finally:
            # Task cancellation mid-stream otherwise leaves the httpx
            # connection open until Agent.close(); close it here.
            await stream.close()

    async def embed(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        return response.data[0].embedding


class AzureOpenAIProvider(OpenAIProvider):
    """Azure OpenAI — uses AsyncAzureOpenAI SDK for proper deployment URL routing."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "",
        model: str = "gpt-4o",
        api_version: str = "2024-08-01-preview",
        ssl_verify: bool = False,
        context_window: int | None = None,
        **_kwargs,
    ) -> None:
        from openai import AsyncAzureOpenAI
        base = endpoint.strip().rstrip("/")
        parsed = urlsplit(base)
        if not base:
            raise ValueError("Azure OpenAI requires an endpoint")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Azure OpenAI endpoint must be an HTTP or HTTPS URL with a hostname")
        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=base,
            azure_deployment=model,
            api_version=api_version,
            http_client=httpx.AsyncClient(verify=ssl_verify, timeout=_http_timeout()),
            max_retries=1,
        )
        self._model = model
        self._display_model = model
        # Per-config override from Settings → AI Models; None = name-matched default.
        self._context_window_override = context_window

    def _is_reasoning_model(self) -> bool:
        m = self._model.lower()
        return any(m.startswith(p) for p in ("o1", "o2", "o3", "o4")) or "gpt-5" in m

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
        )
        # Azure rejects max_completion_tokens for non-reasoning
        # models (gpt-4o and earlier API versions error with 400). Base class
        # splits by reasoning model — Azure must too.
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = Message(
            role="assistant",
            content=choice.message.content or "",
            tool_calls=[
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (choice.message.tool_calls or [])
            ] or None,
        )
        raw_stop = choice.finish_reason
        # Some OpenAI-compatible gateways (vLLM/LM Studio/Ollama) omit usage —
        # guard against AttributeError or compression silently fails forever
        # on that gateway .
        usage = response.usage
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            } if usage is not None else {},
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_openai(raw_stop),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        max_tokens = self._clamp_max_tokens(max_tokens)
        kwargs: dict = dict(
            model=self._model,
            messages=self._to_openai_messages(messages),
            stream=True,
            stream_options={"include_usage": True},
        )
        if self._is_reasoning_model():
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = self._to_openai_tools(tools)

        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice:
                    delta = choice.delta
                    text = delta.content or ""
                    finish = choice.finish_reason
                    normalized = StopReason.from_openai(finish) if finish else None

                    if delta.tool_calls:
                        if text:
                            yield StreamChunk(delta=text, tool_call_delta=None, finish_reason=None)
                        for tc in delta.tool_calls:
                            tc_delta = {
                                "index": tc.index,
                                "id": tc.id or "",
                                "function": {
                                    "name": getattr(tc.function, "name", "") or "",
                                    "arguments": getattr(tc.function, "arguments", "") or "",
                                },
                            }
                            yield StreamChunk(
                                delta="",
                                tool_call_delta=tc_delta,
                                finish_reason=finish,
                                stop_reason=normalized,
                            )
                    else:
                        yield StreamChunk(
                            delta=text,
                            tool_call_delta=None,
                            finish_reason=finish,
                            stop_reason=normalized,
                        )
                elif chunk.usage:
                    yield StreamChunk(
                        usage={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        },
                    )
        finally:
            # Task cancellation mid-stream otherwise leaves the httpx
            # connection open until Agent.close(); close it here.
            await stream.close()
