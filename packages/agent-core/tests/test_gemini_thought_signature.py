"""Gemini 3 thought signatures on function-call replay.

Gemini 4/3 return ``thoughtSignature`` on the functionCall part of each
step and reject the next request in the same turn unless it comes back
verbatim: ``400 INVALID_ARGUMENT. Function call `...` is missing a
thought_signature``. The provider must capture the signature when reading
a response (stream or complete) and write it back when the model turn is
rebuilt in ``_to_gemini_contents``; calls it never saw — a mid-conversation
model switch, a resumed turn on a fresh instance — use the documented
``skip_thought_signature_validator`` placeholder instead.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from google.genai import types as genai_types

from agent_core.providers.base import Message, StopReason
from agent_core.providers.google_gemini import (
    _SKIP_THOUGHT_SIGNATURE,
    GoogleGeminiProvider,
)


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _chunk(parts=None, finish_reason=None, usage=None):
    content = SimpleNamespace(parts=parts) if parts is not None else None
    candidate = SimpleNamespace(content=content, finish_reason=finish_reason)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


def _fc_part(name="get_weather", args=None, signature=None, text=None):
    return SimpleNamespace(
        function_call=SimpleNamespace(name=name, args=args),
        thought_signature=signature,
        thought=False,
        text=text,
    )


def _provider_with_chunks(monkeypatch, chunks) -> GoogleGeminiProvider:
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")

    async def _generate_content_stream(**_kwargs):
        return FakeStream(chunks)

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(
            generate_content_stream=_generate_content_stream,
        )),
    )
    monkeypatch.setattr(provider, "_client", lambda: fake_client)
    return provider


def _tool_loop_messages(call_id: str) -> list[Message]:
    return [
        Message(role="user", content="what's the weather?"),
        Message(
            role="assistant",
            content="",
            tool_calls=[{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
            }],
        ),
        Message(role="tool", content="18C", tool_call_id=call_id, name="get_weather"),
    ]


def _replayed_fc_parts(provider: GoogleGeminiProvider, call_id: str) -> list[dict]:
    _system, contents = provider._to_gemini_contents(_tool_loop_messages(call_id))
    return [p for p in contents[1]["parts"] if "function_call" in p]


async def test_stream_signature_replayed_on_next_request(monkeypatch):
    """The stream path stores the signature under the emitted call id and
    _to_gemini_contents writes it back on the rebuilt functionCall part."""
    sig = b"stream-thought-signature"
    chunks = [
        _chunk(parts=[_fc_part(args={"city": "Paris"}, signature=sig)]),
        _chunk(parts=None, finish_reason=SimpleNamespace(name="STOP")),
    ]
    provider = _provider_with_chunks(monkeypatch, chunks)

    out = [
        c async for c in provider.stream([Message(role="user", content="weather?")])
    ]
    assert out[-1].stop_reason is StopReason.TOOL_USE
    call_id = next(c.tool_call_delta["id"] for c in out if c.tool_call_delta)

    parts = _replayed_fc_parts(provider, call_id)
    assert parts[0]["thought_signature"] == sig


async def test_complete_signature_replayed_on_next_request(monkeypatch):
    sig = b"complete-thought-signature"
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")

    async def _generate_content(**_kwargs):
        return SimpleNamespace(
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[
                    _fc_part(args={"city": "Paris"}, signature=sig),
                ]),
                finish_reason=SimpleNamespace(name="STOP"),
            )],
            usage_metadata=None,
        )

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=_generate_content,
        )),
    )
    monkeypatch.setattr(provider, "_client", lambda: fake_client)

    result = await provider.complete([Message(role="user", content="weather?")])
    assert result.stop_reason is StopReason.TOOL_USE
    call_id = result.message.tool_calls[0]["id"]

    parts = _replayed_fc_parts(provider, call_id)
    assert parts[0]["thought_signature"] == sig


async def test_signature_on_continuation_chunk_lands_on_target_call(monkeypatch):
    """Large arguments split across chunks; the signature may ride the
    continuation part, which carries no id of its own."""
    sig = b"late-thought-signature"
    chunks = [
        _chunk(parts=[_fc_part(args={"city": "Par"})]),
        _chunk(parts=[_fc_part(name="", args={"city": "Paris"}, signature=sig)]),
        _chunk(parts=None, finish_reason=SimpleNamespace(name="STOP")),
    ]
    provider = _provider_with_chunks(monkeypatch, chunks)

    out = [
        c async for c in provider.stream([Message(role="user", content="weather?")])
    ]
    call_id = next(c.tool_call_delta["id"] for c in out if c.tool_call_delta)

    parts = _replayed_fc_parts(provider, call_id)
    assert parts[0]["thought_signature"] == sig


async def test_signature_only_part_after_call_fills_in(monkeypatch):
    """A part carrying only the signature (no functionCall) still belongs
    to the call it follows."""
    sig = b"stray-thought-signature"
    chunks = [
        _chunk(parts=[_fc_part(args={"city": "Paris"})]),
        _chunk(parts=[SimpleNamespace(
            function_call=None, thought_signature=sig, thought=False, text=None,
        )]),
        _chunk(parts=None, finish_reason=SimpleNamespace(name="STOP")),
    ]
    provider = _provider_with_chunks(monkeypatch, chunks)

    out = [
        c async for c in provider.stream([Message(role="user", content="weather?")])
    ]
    call_id = next(c.tool_call_delta["id"] for c in out if c.tool_call_delta)

    parts = _replayed_fc_parts(provider, call_id)
    assert parts[0]["thought_signature"] == sig


async def test_unknown_call_id_gets_skip_placeholder(monkeypatch):
    """Calls this provider instance never generated — history rebuilt after
    a model switch, or a resumed turn — get the documented placeholder."""
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")

    parts = _replayed_fc_parts(provider, "call_neverseen1")
    assert parts[0]["thought_signature"] == _SKIP_THOUGHT_SIGNATURE


async def test_parallel_calls_keep_signature_on_first_only(monkeypatch):
    """Only the first functionCall of a step carries a real signature; the
    second must not be sent bare either (placeholder skips validation)."""
    sig = b"first-parallel-sig"
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")
    provider._thought_signatures["call_aaa111222"] = sig
    messages = [
        Message(role="user", content="check both cities"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                {"id": "call_aaa111222", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
                {"id": "call_bbb333444", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Tokyo"}'}},
            ],
        ),
        Message(role="tool", content="18C", tool_call_id="call_aaa111222", name="get_weather"),
        Message(role="tool", content="22C", tool_call_id="call_bbb333444", name="get_weather"),
    ]

    _system, contents = provider._to_gemini_contents(messages)
    parts = [p for p in contents[1]["parts"] if "function_call" in p]
    assert parts[0]["thought_signature"] == sig
    assert parts[1]["thought_signature"] == _SKIP_THOUGHT_SIGNATURE
    # Parallel results merge into a single user turn of function_response
    # parts (Gemini rejects adjacent same-role contents).
    assert contents[2]["role"] == "user"
    assert len(contents[2]["parts"]) == 2


async def test_replayed_part_serializes_as_thought_signature_on_the_wire():
    """The rebuilt dict must survive the SDK's pydantic layer as the
    camelCase thoughtSignature field with the base64 payload intact."""
    sig = b"wire-thought-signature"
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")
    provider._thought_signatures["call_wire123456"] = sig

    _system, contents = provider._to_gemini_contents(_tool_loop_messages("call_wire123456"))
    dumped = json.loads(genai_types.Content.model_validate(contents[1]).model_dump_json(
        by_alias=True, exclude_none=True,
    ))
    assert dumped["parts"][0]["thoughtSignature"] == base64.b64encode(sig).decode()

    # Placeholder round-trips to itself (it is its own base64 payload).
    provider2 = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")
    _system, contents2 = provider2._to_gemini_contents(_tool_loop_messages("call_x"))
    dumped2 = json.loads(genai_types.Content.model_validate(contents2[1]).model_dump_json(
        by_alias=True, exclude_none=True,
    ))
    assert dumped2["parts"][0]["thoughtSignature"] == _SKIP_THOUGHT_SIGNATURE


async def test_plain_text_turns_carry_no_signature_key():
    """Rebuild stays byte-identical for turns without tool calls."""
    provider = GoogleGeminiProvider(api_key="k", model="gemini-3.1-pro")
    _system, contents = provider._to_gemini_contents([
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
    ])
    assert contents == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
    ]
