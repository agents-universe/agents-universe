"""file_output persistence chain — assistant file attachments end-to-end.

Covers: _persist_assistant_message storing `attachments` in knowledge_refs,
REST history reload surfacing them, and per-user media access for files that
the agent delivered to disk (.tmp/media/).
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select, update

from api.models.conversation import Conversation, Message as DbMessage
from api.paths import PROJECTS_ROOT
from api.websocket.handlers import _persist_assistant_message, _upsert_file_records


async def _make_conversation(client, project_id: str) -> str:
    resp = await client.post(
        f"/api/projects/{project_id}/conversations",
        json={"agent_id": None},
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


async def test_persist_assistant_files_stored_and_serialized(client, db, make_project):
    """A file_output'd attachment survives persist and shows up on GET /messages."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))
    msg_id = str(uuid.uuid4())

    files = [{
        "id": "a1",
        "url": f"/api/media/{project.project_id}/{cid}/ab12cd34.csv",
        "name": "report.csv",
        "media_type": "text/csv",
        "size": 42,
    }]
    await _persist_assistant_message(
        db, cid, "Here is your report", [], msg_id,
        images=None, files=files,
    )

    row = (await db.execute(
        select(DbMessage).where(DbMessage.message_id == msg_id)
    )).scalar_one()
    refs = json.loads(row.knowledge_refs)
    assert refs == {"attachments": files}

    resp = await client.get(f"/api/conversations/{cid}/messages")
    assert resp.status_code == 200
    messages = resp.json()
    msg = next(m for m in messages if m["message_id"] == msg_id)
    assert msg["attachments"] == files
    assert msg["images"] is None


async def test_persist_assistant_images_and_files_together(client, db, make_project):
    """Images and file attachments coexist in one message's knowledge_refs."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))
    msg_id = str(uuid.uuid4())

    await _persist_assistant_message(
        db, cid, "both", [], msg_id,
        images=[{"id": "i1", "url": f"/api/media/{project.project_id}/{cid}/ab12cd34.png", "alt": "shot"}],
        files=[{"id": "a1", "url": f"/api/media/{project.project_id}/{cid}/ab12cd34.csv", "name": "report.csv", "media_type": "text/csv", "size": 42}],
    )

    row = (await db.execute(
        select(DbMessage).where(DbMessage.message_id == msg_id)
    )).scalar_one()
    refs = json.loads(row.knowledge_refs)
    assert set(refs.keys()) == {"images", "attachments"}


async def test_persist_assistant_plain_text_no_refs(client, db, make_project):
    """A message with no outputs keeps knowledge_refs NULL (no behavior change)."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))
    msg_id = str(uuid.uuid4())

    await _persist_assistant_message(db, cid, "plain text", [], msg_id)

    row = (await db.execute(
        select(DbMessage).where(DbMessage.message_id == msg_id)
    )).scalar_one()
    assert row.knowledge_refs is None


async def test_upsert_file_records_same_name_replaces():
    """Re-delivering the same file name updates the link instead of stacking."""
    buf: list[dict] = []
    _upsert_file_records(buf, [
        {"id": "a", "url": "/api/media/p/c/code1.html", "name": "web-slides.html",
         "media_type": "text/html", "size": 10},
        {"id": "b", "url": "/api/media/p/c/code2.csv", "name": "data.csv",
         "media_type": "text/csv", "size": 5},
    ])
    _upsert_file_records(buf, [
        {"id": "c", "url": "/api/media/p/c/code3.html", "name": "web-slides.html",
         "media_type": "text/html", "size": 12},
    ])
    assert [f["name"] for f in buf] == ["web-slides.html", "data.csv"]
    assert buf[0]["id"] == "c" and buf[0]["size"] == 12
    # malformed records are dropped, not appended
    _upsert_file_records(buf, [{"url": "/no-name"}, "junk", {"name": "", "url": "x"}])
    assert len(buf) == 2


async def test_serve_media_agent_file_owner_only(client, db, make_project):
    """Agent-delivered disk files serve to the conversation owner, 404 to others."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    fname = "ab12cd34.csv"
    (media_dir / fname).write_bytes(b"a,b\n1,2\n")
    url = f"/api/media/{project.project_id}/{cid}/{fname}"

    # owner (auth bypass test-user) downloads fine — non-image → octet-stream
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"
    assert resp.headers["content-type"] == "application/octet-stream"

    # Re-own the conversation as another user → 404 (never a 200)
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(user_id="other-user")
    )
    await db.commit()
    resp = await client.get(url)
    assert resp.status_code == 404
