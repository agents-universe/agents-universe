"""pending_prompt_events() — the snapshot the transport replays on (re)connect.

A prompt lives only in the live session and in the client's memory: the
message history carries no trace of it, and the WS `sync` event is the only
channel that can restore the dialog. Without the snapshot, a client that
navigates away and back rebuilds its state from history and shows a
conversation with no dialog while the agent keeps waiting for an answer it
can no longer receive ("切换别的智能体再切回来，确认框看不见了，但对话还在等
待用户输入").
"""
import asyncio
import json

import pytest

from agent_core.session import ConversationSession


async def _drain(s):
    """Drain the session event queue into a list until the None sentinel."""
    events = []
    async for ev in s.events():
        events.append(ev)
    return events


async def test_snapshot_describes_the_prompt_awaiting_input():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection(
            "prompt-1",
            "deploy_target",
            "Which environment?",
            options=[{"label": "dev", "value": "dev"}],
            kind="selection",
            title="Deploy",
        )
    )
    await asyncio.sleep(0.05)  # let it reach the wait

    pending = s.pending_prompt_events()
    assert len(pending) == 1
    assert pending[0]["prompt_id"] == "prompt-1"
    assert pending[0]["field_key"] == "deploy_target"
    assert pending[0]["question"] == "Which environment?"
    assert pending[0]["options"] == [{"label": "dev", "value": "dev"}]
    assert pending[0]["kind"] == "selection"
    assert pending[0]["title"] == "Deploy"
    # The snapshot is a copy — a caller mutating it must not corrupt the
    # payload a later reconnect replays.
    pending[0]["question"] = "mutated"
    assert s.pending_prompt_events()[0]["question"] == "Which environment?"

    s.resolve_user_selection("prompt-1", "dev")
    assert await task == "dev"


async def test_secret_prompt_snapshot_carries_its_save_target():
    """A replayed secret prompt must keep kind/secret/scope flags — the client
    renders a password field and the response must name the save target."""
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection(
            "prompt-secret",
            "api_key",
            "Paste the token",
            kind="text",
            secret=True,
            service_key="vendor",
            environment="prod",
            save_to_project_secrets=True,
        )
    )
    await asyncio.sleep(0.05)

    pending = s.pending_prompt_events()[0]
    assert pending["secret"] is True
    assert pending["save_to_project_secrets"] is True
    assert pending["service_key"] == "vendor"
    assert pending["environment"] == "prod"
    # The payload is what the client is sent — no value/plaintext may ride along.
    assert "value" not in pending

    s.resolve_user_selection_secret("prompt-secret", saved=True)
    assert await task == "secret_saved"


async def test_snapshot_drops_an_answered_prompt():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("prompt-1", "field", "Approve?", timeout=30)
    )
    await asyncio.sleep(0.05)
    assert len(s.pending_prompt_events()) == 1

    s.resolve_user_selection("prompt-1", "yes")
    assert await task == "yes"

    # A reconnect after the answer must not resurrect the dialog.
    assert s.pending_prompt_events() == []


async def test_snapshot_drops_a_timed_out_prompt():
    s = ConversationSession("c1", "p1", "u1")
    with pytest.raises(RuntimeError, match="timed out"):
        await s.request_user_selection("prompt-1", "field", "Approve?", timeout=0.05)
    assert s.pending_prompt_events() == []


async def test_snapshot_drops_an_aborted_prompt():
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection("prompt-1", "field", "Approve?", timeout=30)
    )
    await asyncio.sleep(0.05)

    s.abort()
    with pytest.raises(RuntimeError, match="[Aa]borted"):
        await asyncio.wait_for(task, timeout=2)
    assert s.pending_prompt_events() == []


async def test_the_replayed_event_matches_the_original_one():
    """The sync replay re-uses the emitted payload, so the client can feed it
    through the exact same mapper as the live event."""
    s = ConversationSession("c1", "p1", "u1")
    task = asyncio.create_task(
        s.request_user_selection(
            "prompt-1",
            "field",
            "Approve?",
            options=[{"label": "yes", "value": "yes"}],
            allow_other=False,
            task_id="task-9",
        )
    )
    await asyncio.sleep(0.05)
    await s.close()
    emitted = [ev for ev in await _drain(s) if ev.type == "user_selection_required"]
    assert len(emitted) == 1

    pending = s.pending_prompt_events()
    assert len(pending) == 1
    # Compare as JSON: the event data round-trips through the wire verbatim.
    assert json.loads(json.dumps(pending[0])) == json.loads(json.dumps(emitted[0].data))

    s.resolve_user_selection("prompt-1", "yes")
    assert await task == "yes"
