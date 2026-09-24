"""Headless runs must refuse interactive prompts, not wait out the timeout.

run_turn(interactive=False) (scheduled tasks, published agents) still builds
and registers a ConversationSession — there is simply no client attached, so
a user_selection_response can never arrive. Every prompt gate that only
checked `session is None` (git_repo remove_clone, the MCP destructive gate,
api_request write gate, secret_vault save/delete, skill install, kong token)
therefore stalled the turn for the full 120–300 s before failing. user_confirm
already refuses up front via ToolContext.interactive; the same guarantee now
lives at the session choke point so every current and future caller inherits
it. The check is made on the SINK store: a delegated turn under a headless
parent is refused too, while an interactive parent still answers its
delegates' prompts.
"""
from __future__ import annotations

import time

import pytest

from agent_core.session import ConversationSession

UNATTENDED = "operating unattended"


def _session(interactive: bool, prompt_sink: ConversationSession | None = None) -> ConversationSession:
    s = ConversationSession("c1", "p1", "u1", prompt_sink=prompt_sink)
    s.interactive = interactive
    return s


async def test_noninteractive_prompt_fails_fast_without_emitting():
    session = _session(False)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=UNATTENDED):
        await session.request_user_selection(
            prompt_id="p1",
            field_key="f1",
            question="delete it?",
            timeout=5,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 3, f"waited {elapsed:.1f}s for an answer nobody can give"
    assert session._event_queue.qsize() == 0, "prompt event reached the UI queue"
    assert session._pending_prompts == {}, "prompt left registered"


async def test_nested_prompt_under_headless_parent_is_refused():
    # Prompts register on the sink (top-level) store — that is the session
    # whose client would answer, so ITS interactivity decides.
    parent = _session(False)
    child = _session(True, prompt_sink=parent)
    with pytest.raises(RuntimeError, match=UNATTENDED):
        await child.request_user_selection(
            prompt_id="p2",
            field_key="f2",
            question="allow?",
            timeout=5,
        )
    assert parent._event_queue.qsize() == 0, "prompt event reached the headless parent"


async def test_interactive_prompt_keeps_timeout_behavior():
    session = _session(True)
    with pytest.raises(RuntimeError, match="timed out"):
        await session.request_user_selection(
            prompt_id="p3",
            field_key="f3",
            question="anyone?",
            timeout=0.05,
        )
    assert session._pending_prompts == {}
