"""Upload endpoint tests — POST /api/media/{project_id}/{conversation_id}."""
from __future__ import annotations

import io
import re

from PIL import Image
from sqlalchemy import update

from api.models.conversation import Conversation


async def _make_conversation(client, project_id: str) -> str:
    resp = await client.post(
        f"/api/projects/{project_id}/conversations",
        json={"agent_id": None},
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


def _png_bytes(width: int = 64, height: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 60, 200)).save(buf, format="PNG")
    return buf.getvalue()


async def test_upload_text_file(client, make_project):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    record = resp.json()
    assert record["name"] == "notes.txt"
    assert record["media_type"] == "text/plain"
    assert record["size"] == 11
    assert record["url"].startswith(f"/api/media/{project.project_id}/{cid}/")
    stored_name = record["url"].rsplit("/", 1)[-1]
    # Server-generated uuid name, client filename is display-only
    assert stored_name != "notes.txt"
    assert stored_name.endswith(".txt")
    assert re.fullmatch(r"[0-9a-f]{32}\.txt", stored_name)

    # GET roundtrip serves the uploaded bytes (non-image → octet-stream)
    resp = await client.get(record["url"])
    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_upload_image_with_dimensions(client, make_project):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("shot.png", _png_bytes(64, 32), "image/png")},
    )
    assert resp.status_code == 200
    record = resp.json()
    assert record["media_type"] == "image/png"
    assert record["width"] == 64
    assert record["height"] == 32

    resp = await client.get(record["url"])
    assert resp.headers["content-type"] == "image/png"


async def test_upload_too_large_rejected(client, make_project, monkeypatch):
    from api.config import Settings

    monkeypatch.setattr("api.routers.media.get_settings", lambda: Settings(max_upload_size_mb=1))
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("big.bin", b"x" * (1024 * 1024 + 100), "application/octet-stream")},
    )
    assert resp.status_code == 413

    # Nothing was written to disk (uploads are in-memory only)
    from api.paths import PROJECTS_ROOT
    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    assert not media_dir.exists() or not any(media_dir.iterdir())


async def test_upload_never_written_to_disk(client, make_project):
    """User uploads are held in memory — no file lands in the project workspace."""
    from api.paths import PROJECTS_ROOT
    from api.routers.media import get_upload

    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    stored_name = resp.json()["url"].rsplit("/", 1)[-1]

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    assert not media_dir.exists() or not any(media_dir.iterdir())

    # Bytes are served from the in-memory store
    assert get_upload(cid, stored_name) == b"hello world"
    assert (await client.get(resp.json()["url"])).content == b"hello world"


async def test_upload_other_users_conversation_forbidden(client, db, make_project):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    # Re-own the conversation as another user
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(user_id="other-user")
    )
    await db.commit()

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 404


def test_store_drop_releases_conversation_uploads():
    """Turn-end cleanup drops only the owning conversation's entries."""
    from api.routers.media import drop_uploads, get_upload, list_upload_names, store_upload

    store_upload("c1", "a.txt", b"1")
    store_upload("c1", "b.txt", b"2")
    store_upload("c2", "c.txt", b"3")
    assert sorted(list_upload_names("c1")) == ["a.txt", "b.txt"]

    drop_uploads("c1")
    assert list_upload_names("c1") == []
    assert get_upload("c1", "a.txt") is None
    # Other conversations are untouched
    assert get_upload("c2", "c.txt") == b"3"


async def test_weird_client_filenames_safe(client, make_project):
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    for fname in ("../../evil.txt", "中文 名称.xlsx", "no_ext"):
        resp = await client.post(
            f"/api/media/{project.project_id}/{cid}",
            files={"file": (fname, b"data", "application/octet-stream")},
        )
        assert resp.status_code == 200, fname
        url = resp.json()["url"]
        stored_name = url.rsplit("/", 1)[-1]
        assert ".." not in stored_name, fname
        # Served back fine
        assert (await client.get(url)).status_code == 200, fname
