"""Headless turns must re-check project access at execution time.

Scheduled fires and the published SSE endpoint call run_turn directly, after
the artifact's authoring-time validation. If the user was removed from a
private project in the meantime, the turn must stop before touching the
workspace or project secrets.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from api.models.conversation import Conversation
from api.services.agent_turn import run_turn


class _Transport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, conversation_id: str, data: dict) -> bool:
        self.sent.append(data)
        return True


async def test_headless_turn_denied_after_member_removed_from_private_project(
    db, make_project
):
    project = await make_project(created_by="owner", visibility="private")
    conv = Conversation(
        project_id=project.project_id,
        user_id="removed-user",
        status="active",
        title="scheduled target",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    transport = _Transport()
    await run_turn(
        conv.conversation_id,
        MagicMock(),
        {"type": "message", "content": "run it"},
        "removed-user",
        transport=transport,
        interactive=False,
    )

    denied = [
        event
        for event in transport.sent
        if event.get("message") == "Project access denied"
    ]
    assert denied
    assert denied[0]["terminal"] is True
