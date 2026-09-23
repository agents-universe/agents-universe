"""Anthropic Claude provider."""
from __future__ import annotations

import json
from typing import AsyncIterator

import anthropic
import httpx

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
    thinking_enabled_by_env,
)

def _context_window(model: str) -> int:
    model = model.lower()
    if any(name in model for name in (
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    )):
        return 1_000_000
    # Default for remaining models (haiku-4-5 and any legacy/unknown model)
    return 200_000


# Per-model output ceilings (max_tokens) for the Anthropic Messages API.
# Agent configs default max_tokens to 128000 (agent.py AgentConfig), which
# every Anthropic model rejects with HTTP 400 — the API refuses a ceiling
# above the model's actual output cap. Clamp like the OpenAI provider's
# _clamp_max_tokens, but model-aware: haiku-class caps at 8k, sonnet-class at
# 64k, opus-class at 128k. Unrecognized models get sonnet's 64k — the safest
# ceiling that still fits every known Anthropic family (haiku's 8k would be
# needlessly restrictive for future models, and 64k exceeds no current cap).
def _max_output_tokens(model: str) -> int:
    m = model.lower()
    if "haiku" in m:
        return 8_192
    if "opus" in m:
        return 128_000
    # sonnet-class and any unrecognized model.
    return 64_000


# Families whose current-generation API takes the adaptive thinking param.
# budget_tokens is rejected with HTTP 400 on the 4.6+/5 families — those must
# never receive it. Kept in sync with _context_window's model list.
_ADAPTIVE_THINKING_FAMILIES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def _thinking_rejected(exc: BaseException) -> bool:
    """True when a request failed because of the `thinking` field itself.

    Used for the one-shot retry-without-thinking fallback: corporate gateways
    (Bedrock-style proxies) may reject the param outright. Only the error
    text mentioning "thinking" counts — an unrelated 400 must not silently
    disable thinking.
    """
    text = str(exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            text += " " + resp.text
        except Exception:
            pass
    return "thinking" in text.lower()


class AnthropicClaudeProvider(LLMProvider):
    """Anthropic Claude via the anthropic SDK (direct) or raw httpx (gateway).

    For direct Anthropic API: pass a standard api_key (sk-ant-...).
    For Bedrock-compatible corporate gateways: pass api_key as the Bearer token
    and base_url pointing to the gateway — uses raw httpx (no botocore).
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", base_url: str | None = None, ssl_verify: bool = False, url_mode: str = "base_url", context_window: int | None = None, thinking_enabled: bool | None = None, reasoning_effort: str | None = None) -> None:
        self._model = model
        self._url_mode = url_mode
        # Extended thinking switch: explicit per-config override wins, else
        # AGENT_EXTENDED_THINKING (default on). Also flipped off for the
        # instance lifetime when a gateway rejects the param with a
        # thinking-related 400. reasoning_effort is OpenAI-only — accepted so
        # the provider-agnostic cred dict never TypeErrors a turn.
        self._thinking_enabled = thinking_enabled if thinking_enabled is not None else thinking_enabled_by_env()
        # Per-config override from Settings → AI Models; None = name-matched default.
        self._context_window_override = context_window
        # Exact host comparison, not substring: a gateway whose domain merely
        # CONTAINS "api.anthropic.com" (e.g. api.anthropic.com.evil.example)
        # must not be misdetected as the official API and forced down the
        # AsyncAnthropic SDK path with its different timeout/header behavior
        # .
        from urllib.parse import urlparse
        _host = urlparse(base_url).hostname if base_url else None
        self._is_gateway = bool(_host and _host.lower() != "api.anthropic.com")
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")

        if self._is_gateway:
            # Phased timeout: read must survive large-prompt time-to-first-
            # token; a plain 120s scalar also capped write-heavy bodies.
            self._http = httpx.AsyncClient(
                verify=ssl_verify,
                timeout=httpx.Timeout(300.0, connect=10.0, write=120.0, pool=60.0),
            )
        else:
            import os
            # The Anthropic SDK swapped its HTTP transport from httpx to httpx2
            # in 1.0: a 1.x AsyncAnthropic passed an httpx.AsyncClient raises
            # TypeError("Expected an instance of httpx2.AsyncClient ..."). Pick
            # the transport that matches the installed SDK so a current SDK
            # (CI installs anthropic 1.2) and an older 0.x both keep working.
            try:
                import httpx2
            except ImportError:  # pre-mcp2 environments have no httpx2 package
                httpx2 = None
            try:
                _pairs = (getattr(anthropic, "__version__", "0") or "0").split(".")[:2]
                _uses_httpx2 = tuple(int(p) for p in _pairs) >= (1, 0)
            except (ValueError, TypeError):
                _uses_httpx2 = httpx2 is not None
            _http_lib = httpx2 if _uses_httpx2 and httpx2 is not None else httpx
            http_client = _http_lib.AsyncClient(
                verify=ssl_verify,
                timeout=_http_lib.Timeout(300.0, connect=10.0, write=120.0, pool=60.0),
            )
            # Keep a reference so close() can release it even if SDK init fails
            self._http = http_client
            kwargs: dict = {"api_key": api_key, "http_client": http_client, "max_retries": 1}
            if base_url:
                kwargs["base_url"] = base_url
            _proxy_keys = ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"]
            _saved = {k: os.environ.pop(k) for k in _proxy_keys if k in os.environ}
            try:
                self._client = anthropic.AsyncAnthropic(**kwargs)
            except Exception:
                # Can't await in __init__ — schedule the close if a loop is running
                import asyncio
                try:
                    asyncio.get_running_loop().create_task(http_client.aclose())
                except RuntimeError:
                    pass
                raise
            finally:
                os.environ.update(_saved)

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
        return True

    def _thinking_param(self, max_tokens: int) -> dict | None:
        """Request body `thinking` field for this model, or None when off.

        - 4.6+/5 families take the adaptive shape. `display: "summarized"` is
          load-bearing: the default display is "omitted" on Fable/Mythos/
          Opus 5/Sonnet 5, which streams EMPTY thinking deltas — without it
          the whole thinking UI silently does nothing.
        - haiku-4-5 and older take the legacy budget_tokens shape
          (>=1024 and strictly < max_tokens; never sent to 4.6+/5 — those
          400 on it).
        - Unknown Claude names default to adaptive, the safest against a
          budget-400; a gateway that predates adaptive is covered by the
          retry-without-thinking fallback.
        """
        if not self._thinking_enabled:
            return None
        m = self._model.lower()
        if any(name in m for name in _ADAPTIVE_THINKING_FAMILIES):
            return {"type": "adaptive", "display": "summarized"}
        if "haiku" in m:
            budget = min(4096, max_tokens - 1024)
            if budget < 1024:
                # max_tokens too small to fit a valid budget — skip thinking.
                return None
            return {"type": "enabled", "budget_tokens": budget}
        return {"type": "adaptive", "display": "summarized"}

    def _disable_thinking(self, reason: str) -> None:
        """Turn thinking off for the rest of this provider instance's life."""
        if self._thinking_enabled:
            self._thinking_enabled = False
            import logging
            logging.getLogger(__name__).warning(
                "Extended thinking disabled for %s: %s", self._model, reason,
            )

    def _to_anthropic_messages(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        """Convert to Anthropic format. Returns (system_prompt, messages)."""
        system = None
        out: list[dict] = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content if isinstance(msg.content, str) else str(msg.content)
            elif msg.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
                }
                # parallel tool calls produce consecutive tool
                # messages. Each must not become its own user turn — the API
                # rejects consecutive user messages ("roles must alternate").
                # Merge into the previous user message when it holds only
                # tool_result parts.
                if (
                    out
                    and out[-1]["role"] == "user"
                    and isinstance(out[-1]["content"], list)
                    and all(p.get("type") == "tool_result" for p in out[-1]["content"])
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif msg.role == "assistant" and msg.tool_calls:
                content = []
                # Signed thinking blocks must be replayed first when the
                # assistant turn carries tool_use and thinking is enabled.
                # Only in-memory (same-turn) blocks reach here — signatures
                # are never persisted, so compressed history can't carry a
                # stale block that would 400.
                for blk in msg.thinking_blocks or []:
                    if blk.get("signature"):
                        content.append(blk)
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    # truncated tool calls (stream cut off at
                    # max_tokens) can carry invalid arguments JSON; a throw
                    # here aborts the whole replay. Mirror agent.py's guard:
                    # fall back to {} so the API call still goes out.
                    try:
                        input_args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        input_args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": input_args,
                    })
                out.append({"role": "assistant", "content": content})
            elif isinstance(msg.content, list):
                content = []
                for part in msg.content:
                    if part.get("type") == "image":
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": part["media_type"],
                                "data": part["data"],
                            },
                        })
                    else:
                        content.append({"type": "text", "text": part.get("text", "")})
                out.append({"role": msg.role, "content": content})
            else:
                # Plain assistant text with in-memory thinking blocks: the
                # API wants a block list (thinking first, then text) — a
                # bare string would drop the blocks.
                blocks = [b for b in (msg.thinking_blocks or []) if b.get("signature")]
                if msg.role == "assistant" and blocks:
                    content: list[dict] = list(blocks)
                    if msg.content:
                        content.append({"type": "text", "text": msg.content})
                    out.append({"role": msg.role, "content": content})
                else:
                    out.append({"role": msg.role, "content": msg.content})
        return system, out

    def _to_anthropic_tools(self, tools: list[ToolDefinition]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def _parse_response(self, response) -> CompletionResult:
        text_parts = []
        tool_calls = []
        thinking_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                # Unsigned blocks are display-only and must never be
                # replayed back to the API.
                sig = getattr(block, "signature", None)
                if sig:
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": getattr(block, "thinking", "") or "",
                        "signature": sig,
                    })
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
        msg = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            thinking_blocks=thinking_blocks or None,
        )
        raw_stop = response.stop_reason or "stop"
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
            },
            model=response.model,
            finish_reason=raw_stop,
            stop_reason=StopReason.from_anthropic(raw_stop),
        )

    def _parse_raw_response(self, data: dict) -> CompletionResult:
        """Parse a raw JSON response from the gateway (same schema as Anthropic Messages API)."""
        text_parts = []
        tool_calls = []
        thinking_blocks = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                sig = block.get("signature")
                if sig:
                    thinking_blocks.append({
                        "type": "thinking",
                        "thinking": block.get("thinking", ""),
                        "signature": sig,
                    })
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
        msg = Message(
            role="assistant",
            content="".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            thinking_blocks=thinking_blocks or None,
        )
        usage = data.get("usage", {})
        raw_stop = data.get("stop_reason", "stop")
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
            model=data.get("model", self._model),
            finish_reason=raw_stop,
            stop_reason=StopReason.from_anthropic(raw_stop),
        )

    # ─── Gateway (raw httpx, no botocore) ─────────────────────────────────

    def _gateway_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _gateway_payload(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> dict:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        clamped_max = min(max_tokens, _max_output_tokens(self._model))
        payload: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": clamped_max,
            "temperature": temperature,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._to_anthropic_tools(tools)
        if thinking:
            param = self._thinking_param(clamped_max)
            if param:
                payload["thinking"] = param
        return payload

    async def _gateway_complete(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> CompletionResult:
        if self._url_mode == "full_url":
            url = self._base_url
        else:
            url = f"{self._base_url}/model/{self._model}/invoke"
        payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
        try:
            resp = await self._http.post(url, headers=self._gateway_headers(), json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Gateway rejected the thinking field — retry once without it.
            if thinking and "thinking" in payload and _thinking_rejected(exc):
                self._disable_thinking(f"gateway rejected thinking param: {exc}")
                return await self._gateway_complete(
                    messages, tools, max_tokens, temperature, thinking=False,
                )
            raise
        return self._parse_raw_response(resp.json())

    @staticmethod
    def _completion_to_chunks(result: CompletionResult) -> list[StreamChunk]:
        """Chunks for a non-streaming result replayed through the stream API.

        Thinking text + signature go out first so the agent loop can both
        display them and keep the signed block for the next tool-loop
        request.
        """
        chunks: list[StreamChunk] = []
        for blk in result.message.thinking_blocks or []:
            if blk.get("thinking"):
                chunks.append(StreamChunk(thinking=blk["thinking"]))
            if blk.get("signature"):
                chunks.append(StreamChunk(thinking_signature=blk["signature"]))
        chunks.append(StreamChunk(delta=result.message.content))
        if result.message.tool_calls:
            for i, tc in enumerate(result.message.tool_calls):
                chunks.append(StreamChunk(tool_call_delta={
                    "index": i, "id": tc["id"],
                    "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                }))
        chunks.append(StreamChunk(finish_reason=result.finish_reason, usage=result.usage))
        return chunks

    async def _gateway_stream(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the gateway. Tries SSE streaming first, falls back to non-streaming."""
        if self._url_mode == "full_url":
            url = self._base_url
            payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
            payload["stream"] = True
        else:
            url = f"{self._base_url}/model/{self._model}/invoke-with-response-stream"
            payload = self._gateway_payload(messages, tools, max_tokens, temperature, thinking=thinking)
        headers = self._gateway_headers()

        try:
            async with self._http.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 404:
                    # Gateway doesn't support streaming endpoint — fall back
                    result = await self._gateway_complete(messages, tools, max_tokens, temperature, thinking=thinking)
                    for chunk in self._completion_to_chunks(result):
                        yield chunk
                    return

                if resp.status_code >= 400:
                    # Read the body first so the thinking-rejection check can
                    # inspect it; raise_for_status alone loses the text.
                    await resp.aread()
                    if (
                        thinking and "thinking" in payload
                        and resp.status_code == 400
                        and _thinking_rejected(httpx.HTTPStatusError(
                            "thinking rejected", request=resp.request, response=resp,
                        ))
                    ):
                        self._disable_thinking(f"gateway rejected thinking param: HTTP {resp.status_code}")
                        async for chunk in self._gateway_stream(
                            messages, tools, max_tokens, temperature, thinking=False,
                        ):
                            yield chunk
                        return
                    resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")

                if "text/event-stream" in content_type:
                    async for chunk in self._parse_sse(resp):
                        yield chunk
                else:
                    # Not SSE — read the full body and parse as JSON
                    body = b""
                    async for raw in resp.aiter_bytes():
                        body += raw
                    data = json.loads(body)
                    result = self._parse_raw_response(data)
                    for chunk in self._completion_to_chunks(result):
                        yield chunk

        except httpx.HTTPStatusError:
            # Streaming endpoint failed — fall back to non-streaming
            result = await self._gateway_complete(messages, tools, max_tokens, temperature, thinking=thinking)
            for chunk in self._completion_to_chunks(result):
                yield chunk

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[StreamChunk]:
        """Parse Server-Sent Events from the gateway streaming response."""
        input_tokens = 0
        tool_call_ids: dict[int, str] = {}

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line:
                continue
            # SSE spec allows both "data: <payload>" and "data:<payload>";
            # some gateways/nginx configs emit the latter — matching only the
            # spaced form would silently swallow the whole stream (empty reply,
            # no error).
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    event = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    continue

                event_type = event.get("type", "")

                if event_type == "message_start":
                    msg = event.get("message", {})
                    usage = msg.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)

                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    idx = event.get("index", 0)
                    if block.get("type") == "tool_use":
                        block_id = block.get("id", "")
                        tool_call_ids[idx] = block_id
                        yield StreamChunk(tool_call_delta={
                            "index": idx,
                            "id": block_id,
                            "function": {"name": block.get("name", ""), "arguments": ""},
                        })

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    idx = event.get("index", 0)
                    if delta.get("type") == "text_delta":
                        yield StreamChunk(delta=delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        yield StreamChunk(thinking=delta.get("thinking", ""))
                    elif delta.get("type") == "signature_delta":
                        yield StreamChunk(thinking_signature=delta.get("signature", ""))
                    elif delta.get("type") == "input_json_delta":
                        yield StreamChunk(tool_call_delta={
                            "index": idx,
                            "id": tool_call_ids.get(idx, ""),
                            "function": {"name": "", "arguments": delta.get("partial_json", "")},
                        })

                elif event_type == "message_delta":
                    delta = event.get("delta", {})
                    usage = event.get("usage", {})
                    raw_stop = delta.get("stop_reason")
                    yield StreamChunk(
                        finish_reason=raw_stop,
                        stop_reason=StopReason.from_anthropic(raw_stop) if raw_stop else None,
                        usage={
                            "prompt_tokens": input_tokens,
                            "completion_tokens": usage.get("output_tokens", 0),
                        } if usage else None,
                    )

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._is_gateway:
            await self._http.aclose()
        else:
            await self._client.close()

    # ─── Public API ───────────────────────────────────────────────────────

    def _request_kwargs(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float, thinking: bool = True,
    ) -> dict:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        clamped_max = min(max_tokens, _max_output_tokens(self._model))
        kwargs: dict = dict(
            model=self._model,
            max_tokens=clamped_max,
            temperature=temperature,
            messages=anthropic_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)
        if thinking:
            param = self._thinking_param(clamped_max)
            if param:
                kwargs["thinking"] = param
        return kwargs

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        if self._is_gateway:
            return await self._gateway_complete(messages, tools, max_tokens, temperature)

        kwargs = self._request_kwargs(messages, tools, max_tokens, temperature)
        try:
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:
            # Gateway/proxy rejected the thinking field — retry once without.
            if "thinking" in kwargs and _thinking_rejected(exc):
                self._disable_thinking(f"request rejected thinking param: {exc}")
                kwargs.pop("thinking", None)
                response = await self._client.messages.create(**kwargs)
            else:
                raise
        return self._parse_response(response)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        if self._is_gateway:
            async for chunk in self._gateway_stream(messages, tools, max_tokens, temperature):
                yield chunk
            return

        kwargs = self._request_kwargs(messages, tools, max_tokens, temperature)
        emitted = False
        while True:
            try:
                async with self._client.messages.stream(**kwargs) as stream_ctx:
                    input_tokens = 0
                    tool_call_ids: dict[int, str] = {}

                    async for event in stream_ctx:
                        if hasattr(event, "type"):
                            if event.type == "message_start":
                                msg_usage = getattr(getattr(event, "message", None), "usage", None)
                                if msg_usage:
                                    input_tokens = getattr(msg_usage, "input_tokens", 0)
                            elif event.type == "content_block_start":
                                block = getattr(event, "content_block", None)
                                if block and getattr(block, "type", None) == "tool_use":
                                    block_id = getattr(block, "id", None) or ""
                                    tool_call_ids[event.index] = block_id
                                    yield StreamChunk(tool_call_delta={
                                        "index": event.index,
                                        "id": block_id,
                                        "function": {
                                            "name": block.name,
                                            "arguments": "",
                                        },
                                    })
                                    emitted = True
                            elif event.type == "content_block_delta":
                                delta = event.delta
                                if getattr(delta, "type", None) == "thinking" or hasattr(delta, "thinking"):
                                    # ThinkingDelta has .thinking, not .text —
                                    # check it before the text branch would
                                    # silently drop it.
                                    yield StreamChunk(thinking=delta.thinking)
                                    emitted = True
                                elif getattr(delta, "type", None) == "signature" or hasattr(delta, "signature"):
                                    yield StreamChunk(thinking_signature=delta.signature)
                                    emitted = True
                                elif hasattr(delta, "text"):
                                    yield StreamChunk(delta=delta.text)
                                    emitted = True
                                elif hasattr(delta, "partial_json"):
                                    yield StreamChunk(tool_call_delta={
                                        "index": event.index,
                                        "id": tool_call_ids.get(event.index, ""),
                                        "function": {
                                            "name": "",
                                            "arguments": delta.partial_json,
                                        },
                                    })
                                    emitted = True
                            elif event.type == "message_delta":
                                usage = getattr(event, "usage", None)
                                raw_stop = event.delta.stop_reason
                                yield StreamChunk(
                                    finish_reason=raw_stop,
                                    stop_reason=StopReason.from_anthropic(raw_stop) if raw_stop else None,
                                    usage={
                                        "prompt_tokens": input_tokens,
                                        "completion_tokens": usage.output_tokens if usage else 0,
                                    } if usage else None,
                                )
                                emitted = True
                return
            except Exception as exc:
                # The 400 surfaces when the stream context opens (before any
                # event) — once chunks are out we must not restart the stream
                # or the client would see duplicated content.
                if not emitted and "thinking" in kwargs and _thinking_rejected(exc):
                    self._disable_thinking(f"stream rejected thinking param: {exc}")
                    kwargs.pop("thinking", None)
                    continue
                raise
