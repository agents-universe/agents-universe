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


class AnthropicClaudeProvider(LLMProvider):
    """Anthropic Claude via the anthropic SDK (direct) or raw httpx (gateway).

    For direct Anthropic API: pass a standard api_key (sk-ant-...).
    For Bedrock-compatible corporate gateways: pass api_key as the Bearer token
    and base_url pointing to the gateway — uses raw httpx (no botocore).
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", base_url: str | None = None, ssl_verify: bool = False, url_mode: str = "base_url", context_window: int | None = None) -> None:
        self._model = model
        self._url_mode = url_mode
        # Per-config override from Settings → AI Models; None = name-matched default.
        self._context_window_override = context_window
        # Exact host comparison, not substring: a gateway whose domain merely
        # CONTAINS "api.anthropic.com" (e.g. api.anthropic.com.corp.internal)
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
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
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
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
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
        max_tokens: int, temperature: float,
    ) -> dict:
        system, anthropic_messages = self._to_anthropic_messages(messages)
        payload: dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": min(max_tokens, _max_output_tokens(self._model)),
            "temperature": temperature,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._to_anthropic_tools(tools)
        return payload

    async def _gateway_complete(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float,
    ) -> CompletionResult:
        if self._url_mode == "full_url":
            url = self._base_url
        else:
            url = f"{self._base_url}/model/{self._model}/invoke"
        payload = self._gateway_payload(messages, tools, max_tokens, temperature)
        resp = await self._http.post(url, headers=self._gateway_headers(), json=payload)
        resp.raise_for_status()
        return self._parse_raw_response(resp.json())

    async def _gateway_stream(
        self, messages: list[Message], tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float,
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the gateway. Tries SSE streaming first, falls back to non-streaming."""
        if self._url_mode == "full_url":
            url = self._base_url
            payload = self._gateway_payload(messages, tools, max_tokens, temperature)
            payload["stream"] = True
        else:
            url = f"{self._base_url}/model/{self._model}/invoke-with-response-stream"
            payload = self._gateway_payload(messages, tools, max_tokens, temperature)
        headers = self._gateway_headers()

        try:
            async with self._http.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 404:
                    # Gateway doesn't support streaming endpoint — fall back
                    result = await self._gateway_complete(messages, tools, max_tokens, temperature)
                    yield StreamChunk(delta=result.message.content)
                    if result.message.tool_calls:
                        for i, tc in enumerate(result.message.tool_calls):
                            yield StreamChunk(tool_call_delta={
                                "index": i, "id": tc["id"],
                                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                            })
                    yield StreamChunk(finish_reason=result.finish_reason, usage=result.usage)
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
                    yield StreamChunk(delta=result.message.content)
                    if result.message.tool_calls:
                        for i, tc in enumerate(result.message.tool_calls):
                            yield StreamChunk(tool_call_delta={
                                "index": i, "id": tc["id"],
                                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                            })
                    yield StreamChunk(finish_reason=result.finish_reason, usage=result.usage)

        except httpx.HTTPStatusError:
            # Streaming endpoint failed — fall back to non-streaming
            result = await self._gateway_complete(messages, tools, max_tokens, temperature)
            yield StreamChunk(delta=result.message.content)
            if result.message.tool_calls:
                for i, tc in enumerate(result.message.tool_calls):
                    yield StreamChunk(tool_call_delta={
                        "index": i, "id": tc["id"],
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                    })
            yield StreamChunk(finish_reason=result.finish_reason, usage=result.usage)

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

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        if self._is_gateway:
            return await self._gateway_complete(messages, tools, max_tokens, temperature)

        system, anthropic_messages = self._to_anthropic_messages(messages)
        kwargs: dict = dict(
            model=self._model,
            max_tokens=min(max_tokens, _max_output_tokens(self._model)),
            temperature=temperature,
            messages=anthropic_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        response = await self._client.messages.create(**kwargs)
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

        system, anthropic_messages = self._to_anthropic_messages(messages)
        kwargs: dict = dict(
            model=self._model,
            max_tokens=min(max_tokens, _max_output_tokens(self._model)),
            temperature=temperature,
            messages=anthropic_messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

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
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            yield StreamChunk(delta=delta.text)
                        elif hasattr(delta, "partial_json"):
                            yield StreamChunk(tool_call_delta={
                                "index": event.index,
                                "id": tool_call_ids.get(event.index, ""),
                                "function": {
                                    "name": "",
                                    "arguments": delta.partial_json,
                                },
                            })
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
