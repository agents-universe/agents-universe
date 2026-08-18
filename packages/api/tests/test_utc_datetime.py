"""UTC datetime round-trip: naive DB storage, aware ORM reads, offset ISO output.

Regression guard for the frontend misreading offset-less ISO strings as
local time (conversation history off by the browser's UTC offset).
"""
from __future__ import annotations

from datetime import datetime, timezone

from api.database import AsyncSessionLocal
from api.models.conversation import Conversation, Message
from api.routers.conversations import serialize_message


async def test_roundtrip_reads_back_aware_utc(db, make_project):
    project = await make_project()
    conv = Conversation(project_id=str(project.project_id), user_id="test-user")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    assert conv.created_at.tzinfo is not None
    assert conv.created_at.isoformat().endswith("+00:00")


async def test_naive_value_serialized_with_utc_offset(db, make_project):
    project = await make_project()
    conv = Conversation(project_id=str(project.project_id), user_id="test-user")
    db.add(conv)
    await db.commit()
    await db.refresh(conv)

    msg = Message(
        conversation_id=str(conv.conversation_id),
        role="user",
        content="hi",
        sequence_num=1,
        created_at=datetime(2026, 1, 2, 3, 4, 5),  # naive UTC wall-clock
    )
    db.add(msg)
    await db.commit()

    # Fresh session re-reads the stored row through the UTC type.
    async with AsyncSessionLocal() as fresh:
        loaded = await fresh.get(Message, str(msg.message_id))
        assert loaded.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert serialize_message(loaded)["created_at"] == "2026-01-02T03:04:05+00:00"
