"""StopReason normalization — every raw provider value the agent acts on must
map to its normalized form; an unmapped value silently becomes UNKNOWN, which
the run loop treats as a normal end_turn."""
from __future__ import annotations

import pytest

from agent_core.providers.base import StopReason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("stop_sequence", StopReason.END_TURN),
        ("tool_use", StopReason.TOOL_USE),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("pause_turn", StopReason.PAUSE_TURN),
        ("refusal", StopReason.REFUSAL),
        # Request exceeded the model's context window: without this mapping
        # the agent ended the turn as if the model were done and never emitted
        # context_exceeded (which drives the compress-and-retry path).
        ("model_context_window_exceeded", StopReason.CONTEXT_EXCEEDED),
        ("something_new", StopReason.UNKNOWN),
        (None, StopReason.UNKNOWN),
    ],
)
def test_from_anthropic_maps_every_known_reason(raw, expected):
    assert StopReason.from_anthropic(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", StopReason.END_TURN),
        ("tool_calls", StopReason.TOOL_USE),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.CONTENT_FILTER),
        ("something_new", StopReason.UNKNOWN),
        (None, StopReason.UNKNOWN),
    ],
)
def test_from_openai_maps_every_known_reason(raw, expected):
    assert StopReason.from_openai(raw) is expected
