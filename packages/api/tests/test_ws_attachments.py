"""Attachment helper unit tests — validation, preparation, history rehydration."""
from __future__ import annotations

import io
import json
import uuid

from PIL import Image
from sqlalchemy import select

from api.models.conversation import Conversation, Message as DbMessage
from api.paths import PROJECTS_ROOT
from api.websocket.handlers import (
    _MAX_INLINE_TEXT_CHARS,
    _load_history,
    _prepare_attachment,
    _validate_attachment_url,
)


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 60, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def _make_conversation(client, project_id: str) -> str:
    resp = await client.post(
        f"/api/projects/{project_id}/conversations",
        json={"agent_id": None},
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


# --- _validate_attachment_url -------------------------------------------------


async def test_validate_url_accepts_valid(client, make_project, tmp_path):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))
    (tmp_path / "abc123.txt").write_text("hi", encoding="utf-8")

    path = _validate_attachment_url(
        {"url": f"/api/media/{project.project_id}/{cid}/abc123.txt"},
        str(project.project_id),
        cid,
        tmp_path,
    )
    assert path is not None and path.name == "abc123.txt"


async def test_validate_url_rejects_cross_context(client, make_project, tmp_path):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))
    (tmp_path / "abc123.txt").write_text("hi", encoding="utf-8")

    cases = [
        # wrong project
        f"/api/media/other-project/{cid}/abc123.txt",
        # wrong conversation
        f"/api/media/{project.project_id}/other-conv/abc123.txt",
        # extra segments
        f"/api/media/{project.project_id}/{cid}/sub/abc123.txt",
        # not a media url at all
        "/api/whatever/abc123.txt",
    ]
    for url in cases:
        assert _validate_attachment_url(
            {"url": url}, str(project.project_id), cid, tmp_path
        ) is None, url


async def test_validate_url_rejects_missing_or_unsafe(client, make_project, tmp_path):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    # file does not exist
    assert _validate_attachment_url(
        {"url": f"/api/media/{project.project_id}/{cid}/nope.txt"},
        str(project.project_id),
        cid,
        tmp_path,
    ) is None

    # traversal attempt
    (tmp_path / "safe.txt").write_text("hi", encoding="utf-8")
    assert _validate_attachment_url(
        {"url": f"/api/media/{project.project_id}/{cid}/../safe.txt"},
        str(project.project_id),
        cid,
        tmp_path,
    ) is None


# --- _prepare_attachment -------------------------------------------------------


async def test_prepare_text_inline_and_truncation(tmp_path):
    txt = tmp_path / "data.csv"
    # write_bytes: on Windows write_text() would translate \n -> \r\n and the
    # handler reads raw bytes, breaking the round-trip comparison
    txt.write_bytes(b"a,b,c\n1,2,3\n")
    prepared = await _prepare_attachment(
        {"id": "a1", "url": "u", "name": "data.csv", "media_type": "text/csv", "size": 1},
        txt,
        "conv-1",
    )
    assert prepared["inline_text"] == "a,b,c\n1,2,3\n"
    assert prepared["rel_path"] == ".tmp/media/conv-1/data.csv"

    long_txt = tmp_path / "long.log"
    long_txt.write_text("x" * (_MAX_INLINE_TEXT_CHARS + 5000), encoding="utf-8")
    prepared = await _prepare_attachment(
        {"id": "a2", "url": "u", "name": "long.log", "media_type": "text/plain", "size": 1},
        long_txt,
        "conv-1",
    )
    assert len(prepared["inline_text"]) == _MAX_INLINE_TEXT_CHARS + len("\n[... truncated ...]")
    assert prepared["inline_text"].endswith("[... truncated ...]")


async def test_prepare_binary_no_inline(tmp_path):
    binary = tmp_path / "book.xlsx"
    binary.write_bytes(b"PK\x03\x04 fake xlsx bytes")
    prepared = await _prepare_attachment(
        {"id": "a3", "url": "u", "name": "book.xlsx", "media_type": "application/octet-stream", "size": 19},
        binary,
        "conv-1",
    )
    assert "inline_text" not in prepared
    assert "image_data" not in prepared


async def test_prepare_upload_any_utf8_suffix_gets_inline(tmp_path):
    """In-memory uploads of any UTF-8 file (e.g. .ps1, .bat) get inline text,
    not just _TEXTISH_SUFFIXES — the agent must not need a tool read."""
    script = tmp_path / "deploy.ps1"
    script.write_bytes(b"Write-Host 'hello'\r\n")
    prepared = await _prepare_attachment(
        {"id": "a6", "url": "u", "name": "deploy.ps1", "media_type": "text/plain", "size": 20},
        script,
        "conv-1",
        data=b"Write-Host 'hello'\r\n",
    )
    assert prepared["inline_text"] == "Write-Host 'hello'\r\n"
    assert prepared["rel_path"] == ".tmp/media/conv-1/deploy.ps1"


async def test_prepare_upload_binary_falls_back_to_path_ref(tmp_path):
    """Binary uploads (not UTF-8) degrade to a path reference — no inline text."""
    blob = tmp_path / "data.xlsx"
    blob.write_bytes(b"PK\x03\x04" + bytes(range(128, 256)))
    prepared = await _prepare_attachment(
        {"id": "a7", "url": "u", "name": "data.xlsx", "media_type": "application/octet-stream", "size": 130},
        blob,
        "conv-1",
        data=b"PK\x03\x04" + bytes(range(128, 256)),
    )
    assert "inline_text" not in prepared
    assert "image_data" not in prepared
    assert prepared["rel_path"] == ".tmp/media/conv-1/data.xlsx"


async def test_prepare_image_base64(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(_png_bytes())
    prepared = await _prepare_attachment(
        {"id": "a4", "url": "u", "name": "shot.png", "media_type": "image/png", "size": 1},
        img,
        "conv-1",
    )
    assert prepared["image_data"]
    assert prepared["image_media_type"] == "image/png"
    assert "inline_text" not in prepared


async def test_prepare_large_image_no_base64(tmp_path):
    img = tmp_path / "huge.png"
    img.write_bytes(b"\x89PNG" + b"x" * (3 * 1024 * 1024 + 1))
    prepared = await _prepare_attachment(
        {"id": "a5", "url": "u", "name": "huge.png", "media_type": "image/png", "size": 1},
        img,
        "conv-1",
    )
    assert "image_data" not in prepared


# --- _load_history rehydration -------------------------------------------------


async def test_load_history_rehydrates_attachments(db, make_project):
    project = await make_project()
    cid = str(uuid.uuid4())
    db.add(Conversation(conversation_id=cid, project_id=str(project.project_id), user_id="test-user"))
    await db.flush()

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "notes.txt").write_text("hello notes", encoding="utf-8")
    (media_dir / "shot.png").write_bytes(_png_bytes())

    refs = json.dumps({
        "images": [{"id": "i1", "url": f"/api/media/{project.project_id}/{cid}/shot.png", "alt": "shot.png"}],
        "attachments": [{
            "id": "a1",
            "url": f"/api/media/{project.project_id}/{cid}/notes.txt",
            "name": "notes.txt",
            "media_type": "text/plain",
            "size": 11,
        }],
    })
    db.add(DbMessage(
        conversation_id=cid,
        role="user",
        content="看一下这两个附件",
        knowledge_refs=refs,
        sequence_num=1,
    ))
    await db.commit()

    history = await _load_history(db, cid, media_dir)
    assert len(history) == 1
    msg = history[0]
    assert isinstance(msg.content, list)
    types = [p["type"] for p in msg.content]
    # text parts first (original + inline attachment), then the image part
    assert types == ["text", "text", "image"]
    assert "看一下这两个附件" in msg.content[0]["text"]
    assert "hello notes" in msg.content[1]["text"]
    assert msg.content[2]["media_type"] == "image/png"
    assert msg.content[2]["data"]


async def test_load_history_degrades_when_file_missing(db, make_project):
    project = await make_project()
    cid = str(uuid.uuid4())
    db.add(Conversation(conversation_id=cid, project_id=str(project.project_id), user_id="test-user"))
    await db.flush()

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    refs = json.dumps({
        "images": [{"id": "i1", "url": f"/api/media/{project.project_id}/{cid}/gone.png", "alt": "gone.png"}],
        "attachments": [{
            "id": "a1",
            "url": f"/api/media/{project.project_id}/{cid}/missing.txt",
            "name": "missing.txt",
            "media_type": "text/plain",
            "size": 11,
        }],
    })
    db.add(DbMessage(
        conversation_id=cid,
        role="user",
        content="附件还在吗",
        knowledge_refs=refs,
        sequence_num=1,
    ))
    await db.commit()

    history = await _load_history(db, cid, media_dir)
    msg = history[0]
    assert isinstance(msg.content, list)
    # Everything degrades to text refs with file paths — no crash, no image parts
    assert all(p["type"] == "text" for p in msg.content)
    assert "file path:" in msg.content[-1]["text"]


async def test_load_history_rehydration_turn_cap(db, make_project):
    """Only the most recent _REHYDRATE_IMAGE_TURNS attachment turns get vision."""
    from api.websocket.handlers import _REHYDRATE_IMAGE_TURNS

    project = await make_project()
    cid = str(uuid.uuid4())
    db.add(Conversation(conversation_id=cid, project_id=str(project.project_id), user_id="test-user"))
    await db.flush()

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    img_bytes = _png_bytes()

    # 5 user messages each with an image attachment (turn 1 = oldest)
    for i in range(_REHYDRATE_IMAGE_TURNS + 1):
        (media_dir / f"img{i}.png").write_bytes(img_bytes)
        db.add(DbMessage(
            conversation_id=cid,
            role="user",
            content=f"附件 {i}",
            knowledge_refs=json.dumps({
                "images": [{"id": f"i{i}", "url": f"/api/media/{project.project_id}/{cid}/img{i}.png", "alt": f"img{i}.png"}],
                "attachments": [],
            }),
            sequence_num=i + 1,
        ))
    await db.commit()

    history = await _load_history(db, cid, media_dir)
    image_turns = sum(
        1 for m in history if isinstance(m.content, list) and any(p["type"] == "image" for p in m.content)
    )
    assert image_turns == _REHYDRATE_IMAGE_TURNS
    # The vision budget must land on the NEWEST turns — the OLDEST turn
    # (附件 0) degrades to a text ref while the last _REHYDRATE_IMAGE_TURNS
    # turns hydrate.
    oldest = history[0]
    assert isinstance(oldest.content, list)
    assert not any(p["type"] == "image" for p in oldest.content)
    newest = history[-1]
    assert isinstance(newest.content, list)
    assert any(p["type"] == "image" for p in newest.content)
