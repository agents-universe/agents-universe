"""Malformed messages.tool_calls rows must not kill the next turn.

The column is free-form JSON written by older/looser code paths. _load_history
already tolerates invalid JSON and non-iterable payloads, but a JSON object or
string iterates into strings — the per-item ``.get`` then raised
AttributeError, which escaped the (ValueError, TypeError) catch.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from api.models.conversation import Conversation, Message as DbMessage
from api.services.agent_turn import _load_history


def _conversation(db, project) -> Conversation:
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id="test-user",
    )
    db.add(conv)
    return conv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"not": "a list"}',   # iterates into keys
        '"a bare string"',     # iterates into characters
    ],
)
async def test_load_history_skips_malformed_tool_calls(db, make_project, payload):
    project = await make_project()
    conv = _conversation(db, project)
    db.add(DbMessage(
        conversation_id=conv.conversation_id,
        role="assistant",
        content="reply",
        sequence_num=1,
        tool_calls=payload,
    ))
    await db.commit()

    history = await _load_history(db, conv.conversation_id, Path("."))

    assert [m.content for m in history if m.role == "assistant"] == ["reply"]
