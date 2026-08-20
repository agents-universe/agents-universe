"""Google Gemini provider via the google-genai SDK.

The previous google.generativeai package is EOL (no fixes since 2024); this
module targets its successor google-genai. The new SDK's Client is
instance-scoped, so the old genai.configure() process-global key (and the
request lock that serialized configure + request) is gone entirely.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import AsyncIterator

from google import genai
from google.genai import types as genai_types

from .base import (
    CompletionResult,
    LLMProvider,
    Message,
    StopReason,
    StreamChunk,
    ToolDefinition,
)

# Cleared around Client construction: google-genai builds its httpx client
# there and would otherwise read the ambient proxy env (same behavior the
# old genai.configure path enforced).
_PROXY_KEYS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
]


def _continuation_target_index(
    seen_arg_keys: dict[int, set[str]], chunk_args: dict, fallback: int
) -> int:
    """Pick which tool call a nameless continuation chunk continues.

    Gemini splits large arguments across chunks; the continuation parts carry
    an empty name, so "the previous call" is not necessarily the one being
    continued when parallel function calls interleave. Match by argument-key
    overlap with each call's accumulated args; with no overlap anywhere, fall
    back to the most recent call (previous behavior).
    """
    chunk_keys = set(chunk_args)
    target = fallback
    best = 0
    for idx, keys in seen_arg_keys.items():
        overlap = len(keys & chunk_keys)
        if overlap > best:
            best = overlap
            target = idx
    return target


def _context_window(model: str) -> int:
    m = model.lower()
    if "ultra" in m:
        return 32_768
    # All Gemini 1.5+ and 2.x+ models have 1M context
    return 1_048_576


def _part_text(part) -> str | None:
    """Part.text raises ValueError when the part carries no text field
    (unlike the old SDK, which returned None)."""
    try:
        return part.text
    except ValueError:
        return None


class GoogleGeminiProvider(LLMProvider):
    """Google Gemini via the google-genai SDK."""

    def __init__(self, api_key: str, model: str = "gemini-1.5-flash", base_url: str | None = None, url_mode: str = "base_url", context_window: int | None = None, ssl_verify: bool = False) -> None:
        self._api_key = api_key
        self._model_name = model
        self._base_url = base_url
        self._ssl_verify = ssl_verify
        # Per-config override from Settings -> AI Models; None = name-matched default.
        self._context_window_override = context_window

    def _client(self) -> genai.Client:
        """Instance-scoped client; a fresh one per call keeps key/endpoint
        state off the process globals (google-genai has none anyway)."""
        http_options = genai_types.HttpOptions(
            **({"base_url": self._base_url.rstrip("/")} if self._base_url else {}),
            client_args={"verify": self._ssl_verify},
        )
        saved = {k: os.environ.pop(k) for k in _PROXY_KEYS if k in os.environ}
        try:
            return genai.Client(api_key=self._api_key, http_options=http_options)
        finally:
            os.environ.update(saved)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def context_window(self) -> int:
        return self._context_window_override or _context_window(self._model_name)

    @property
    def supports_tool_calls(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return True

    def _to_gemini_contents(self, messages: list[Message]) -> tuple[str | None, list]:
        system = None
        contents = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content if isinstance(msg.content, str) else str(msg.content)
                continue
            if msg.role == "tool":
                part = {
                    "function_response": {
                        "name": msg.name or "",
                        "response": {"result": msg.content if isinstance(msg.content, str) else json.dumps(msg.content)},
                    }
                }
                # parallel tool calls produce consecutive tool
                # messages. Each must not become its own user turn - Gemini
                # rejects alternating violations from adjacent user contents
                # carrying function_response parts. Merge into the previous
                # user content when it holds only function_response parts.
                if (
                    contents
                    and contents[-1]["role"] == "user"
                    and all("function_response" in p for p in contents[-1]["parts"])
                ):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            if msg.role == "assistant" and msg.tool_calls:
                parts = []
                if msg.content:
                    parts.append({"text": msg.content})
                for tc in msg.tool_calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    parts.append({
                        "function_call": {
                            "name": fn.get("name", ""),
                            "args": args,
                        }
                    })
                contents.append({"role": "model", "parts": parts})
                continue
            role = "model" if msg.role == "assistant" else "user"
            if isinstance(msg.content, str):
                contents.append({"role": role, "parts": [{"text": msg.content}]})
            else:
                parts = []
                for part in msg.content:
                    if part.get("type") == "image":
                        parts.append({
                            "inline_data": {
                                "mime_type": part["media_type"],
                                "data": part["data"],
                            }
                        })
                    else:
                        parts.append({"text": part.get("text", "")})
                contents.append({"role": role, "parts": parts})
        return system, contents

    def _to_gemini_tools(self, tools: list[ToolDefinition]):
        declarations = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tools
        ]
        return [genai_types.Tool(function_declarations=declarations)]

    def _request_config(
        self, system: str | None, tools: list[ToolDefinition] | None,
        max_tokens: int, temperature: float,
    ) -> genai_types.GenerateContentConfig:
        return genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=temperature,
            system_instruction=system or None,
            tools=self._to_gemini_tools(tools) if tools else None,
        )

    @staticmethod
    def _empty_result(model: str) -> CompletionResult:
        return CompletionResult(
            message=Message(role="assistant", content="", tool_calls=None),
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            model=model,
            finish_reason="error",
            stop_reason=StopReason.UNKNOWN,
        )

    @staticmethod
    def _normalize_stop(fr_name: str, has_tool_calls: bool) -> StopReason:
        # finish_reason may be MAX_TOKENS (output limit hit) or SAFETY/
        # RECITATION (content blocked) - masking it as a normal END_TURN
        # would end the turn silently: no truncation warning, no refusal
        # signal, the UI never learns the reply was cut off.
        if has_tool_calls:
            return StopReason.TOOL_USE
        if fr_name == "MAX_TOKENS":
            return StopReason.MAX_TOKENS
        if fr_name in ("SAFETY", "RECITATION"):
            return StopReason.REFUSAL
        return StopReason.END_TURN

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CompletionResult:
        system, contents = self._to_gemini_contents(messages)
        client = self._client()
        response = await client.aio.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=self._request_config(system, tools, max_tokens, temperature),
        )
        if not response.candidates:
            return self._empty_result(self._model_name)
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return self._empty_result(self._model_name)
        text_parts = []
        tool_calls = []
        for part in candidate.content.parts:
            fc = part.function_call
            if fc:
                # Gemini occasionally emits a function_call part with
                # an empty name (mirrors the stream path's continuation
                # chunks). A nameless call would surface as "Unknown tool:"
                # in the loop - drop it instead.
                if not (fc.name or "").strip():
                    continue
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {"name": fc.name, "arguments": json.dumps(dict(fc.args))},
                })
                continue
            text = _part_text(part)
            if text:
                text_parts.append(text)
        msg = Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls or None)
        usage = response.usage_metadata
        fr_name = str(getattr(candidate.finish_reason, "name", ""))
        return CompletionResult(
            message=msg,
            usage={
                "prompt_tokens": usage.prompt_token_count if usage else 0,
                "completion_tokens": usage.candidates_token_count if usage else 0,
            },
            model=self._model_name,
            finish_reason=fr_name,
            stop_reason=self._normalize_stop(fr_name, bool(tool_calls)),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> AsyncIterator[StreamChunk]:
        system, contents = self._to_gemini_contents(messages)
        client = self._client()
        chunk_stream = await client.aio.models.generate_content_stream(
            model=self._model_name,
            contents=contents,
            config=self._request_config(system, tools, max_tokens, temperature),
        )
        tool_call_index = 0
        has_tool_calls = False
        last_usage = None
        # Gemini streams a terminal chunk whose finish_reason reports why the
        # generation ended (MAX_TOKENS on the output limit, SAFETY/RECITATION
        # on a content block) - without it every cut-off reply would read as
        # a normal end of turn.
        last_finish_reason = None
        # Argument-key fingerprint per call index: nameless continuation
        # chunks are matched to the call they continue via key overlap
        # (see _continuation_target_index) instead of assuming the most
        # recent call - parallel calls interleave their continuation chunks.
        seen_arg_keys: dict[int, set[str]] = {}
        async for chunk in chunk_stream:
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is not None:
                last_finish_reason = finish_reason
            if not candidate.content or not candidate.content.parts:
                continue
            for part in candidate.content.parts:
                fc = part.function_call
                if fc:
                    name = (fc.name or "").strip()
                    args_json = json.dumps(dict(fc.args)) if fc.args else ""
                    if name:
                        # First (or only) chunk of a function call.
                        seen_arg_keys[tool_call_index] = set(dict(fc.args)) if fc.args else set()
                        yield StreamChunk(
                            delta="",
                            tool_call_delta={
                                "index": tool_call_index,
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "function": {"name": name, "arguments": args_json},
                            },
                        )
                        # has_tool_calls must be set only when a delta was
                        # actually emitted - a nameless function_call part at
                        # index 0 (skipped below) is dropped as unparseable
                        # and must not report finish_reason="tool_calls" with
                        # nothing to consume.
                        has_tool_calls = True
                        tool_call_index += 1
                    elif tool_call_index > 0:
                        # Continuation chunk: Gemini splits large arguments
                        # across chunks with an empty name - emit under the
                        # matched call's index so the loop merges the fragments.
                        chunk_args = dict(fc.args) if fc.args else {}
                        target = _continuation_target_index(
                            seen_arg_keys, chunk_args, tool_call_index - 1
                        )
                        if chunk_args and target in seen_arg_keys:
                            seen_arg_keys[target] |= set(chunk_args)
                        yield StreamChunk(
                            delta="",
                            tool_call_delta={
                                "index": target,
                                "function": {"name": "", "arguments": args_json},
                            },
                        )
                    continue
                text = _part_text(part)
                if text:
                    yield StreamChunk(delta=text)
            if chunk.usage_metadata:
                last_usage = chunk.usage_metadata
        usage_dict = None
        if last_usage:
            usage_dict = {
                "prompt_tokens": getattr(last_usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(last_usage, "candidates_token_count", 0) or 0,
            }
        if has_tool_calls:
            yield StreamChunk(finish_reason="tool_calls", stop_reason=StopReason.TOOL_USE, usage=usage_dict)
        else:
            # Surface the terminal chunk's finish_reason so the loop can warn
            # about truncation / refusal instead of ending the turn silently.
            fr_name = str(getattr(last_finish_reason, "name", ""))
            if fr_name == "MAX_TOKENS":
                yield StreamChunk(finish_reason="max_tokens", stop_reason=StopReason.MAX_TOKENS, usage=usage_dict)
            elif fr_name in ("SAFETY", "RECITATION"):
                yield StreamChunk(finish_reason="refusal", stop_reason=StopReason.REFUSAL, usage=usage_dict)
            else:
                yield StreamChunk(finish_reason="stop", stop_reason=StopReason.END_TURN, usage=usage_dict)

    async def embed(self, text: str) -> list[float]:
        client = self._client()
        result = await client.aio.models.embed_content(
            model="text-embedding-004",
            contents=text,
        )
        return list(result.embeddings[0].values)
