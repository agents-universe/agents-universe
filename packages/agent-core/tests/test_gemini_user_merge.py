"""Gemini contents: consecutive user messages must merge into ONE turn.

Gemini rejects requests whose ``contents`` carry adjacent ``user`` roles
("Please ensure that multiturn requests alternate between user and model
turns"). The tool-results merge only covered function_response parts, but
three reachable paths still produced adjacent user turns: a mid-run
injection landing right after the tool-results turn, two queued injections
appended back to back, and a persisted injection rebuilt after the
synthesized tool rows on every later turn of that conversation.
"""
from __future__ import annotations

from agent_core.providers.base import Message
from agent_core.providers.google_gemini import GoogleGeminiProvider


def _provider() -> GoogleGeminiProvider:
    return GoogleGeminiProvider(api_key="k", model="gemini-2.0-flash")


def test_two_consecutive_user_texts_merge_into_one_turn():
    provider = _provider()
    messages = [
        Message(role="user", content="question"),
        Message(role="user", content="injected follow-up"),
    ]

    _system, contents = provider._to_gemini_contents(messages)

    assert [c["role"] for c in contents] == ["user"]
    assert [p.get("text") for p in contents[0]["parts"]] == [
        "question", "injected follow-up",
    ]


def test_user_text_after_tool_results_stays_alternating():
    provider = _provider()
    messages = [
        Message(role="user", content="do the work"),
        Message(
            role="assistant", content=None,
            tool_calls=[{"id": "c1", "function": {"name": "search", "arguments": "{}"}}],
        ),
        Message(role="tool", content="found it", tool_call_id="c1", name="search"),
        Message(role="user", content="actually focus on X instead"),
    ]

    _system, contents = provider._to_gemini_contents(messages)

    # tool + injection text share ONE user turn: user → model → user.
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    last = contents[-1]
    assert any("function_response" in p for p in last["parts"])
    assert any(p.get("text") == "actually focus on X instead" for p in last["parts"])


def test_user_after_model_does_not_merge():
    provider = _provider()
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="q2"),
    ]

    _system, contents = provider._to_gemini_contents(messages)

    assert [c["role"] for c in contents] == ["user", "model", "user"]


def test_vision_user_message_merges_parts_too():
    provider = _provider()
    multimodal = [
        {"type": "image", "media_type": "image/png", "data": "aW1n"},
        {"type": "text", "text": "what is this"},
    ]
    messages = [
        Message(role="user", content="first"),
        Message(role="user", content=multimodal),
    ]

    _system, contents = provider._to_gemini_contents(messages)

    assert len(contents) == 1
    kinds = sorted(
        "inline_data" if "inline_data" in p else "text" for p in contents[0]["parts"]
    )
    assert kinds == ["inline_data", "text", "text"]
