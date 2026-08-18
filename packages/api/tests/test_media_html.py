"""HTML media serving — .html/.htm served inline as text/html.

The office/web-slides skill delivers self-contained reveal.js presentations;
the media endpoint must render them in the browser (text/html) instead of
forcing a download (octet-stream). Non-HTML suffixes keep the old behavior.
"""
from __future__ import annotations

from sqlalchemy import update

from api.models.conversation import Conversation
from api.paths import PROJECTS_ROOT


async def _make_conversation(client, project_id: str) -> str:
    resp = await client.post(
        f"/api/projects/{project_id}/conversations",
        json={"agent_id": None},
    )
    assert resp.status_code == 200
    return resp.json()["conversation_id"]


async def test_serve_media_disk_html_inline(client, db, make_project):
    """Agent-delivered .html/.htm on disk render inline (text/html)."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "ab12cd34.html").write_bytes(b"<!DOCTYPE html><h1>slides</h1>")
    (media_dir / "ab12cd34.htm").write_bytes(b"<!DOCTYPE html><h1>slides</h1>")

    for fname in ("ab12cd34.html", "ab12cd34.htm"):
        resp = await client.get(f"/api/media/{project.project_id}/{cid}/{fname}")
        assert resp.status_code == 200
        # Starlette appends charset=utf-8 to text/* media types
        assert resp.headers["content-type"].startswith("text/html")


async def test_serve_media_disk_html_owner_only(client, db, make_project):
    """HTML media still serves to the conversation owner only — 404 to others."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "ab12cd34.html").write_bytes(b"<!DOCTYPE html><h1>slides</h1>")
    url = f"/api/media/{project.project_id}/{cid}/ab12cd34.html"

    resp = await client.get(url)
    assert resp.status_code == 200

    # Re-own the conversation as another user → 404 (never a 200)
    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(user_id="other-user")
    )
    await db.commit()
    resp = await client.get(url)
    assert resp.status_code == 404


async def test_upload_html_served_inline(client, make_project):
    """Uploaded .html gets text/html on both the record and the GET response."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("deck.html", b"<!DOCTYPE html><h1>slides</h1>", "text/html")},
    )
    assert resp.status_code == 200
    record = resp.json()
    assert record["media_type"] == "text/html"

    resp = await client.get(record["url"])
    assert resp.status_code == 200
    assert resp.content == b"<!DOCTYPE html><h1>slides</h1>"
    assert resp.headers["content-type"].startswith("text/html")


async def test_serve_media_disk_csv_still_octet_stream(client, make_project):
    """Regression: non-HTML suffixes keep the download behavior."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "ab12cd34.csv").write_bytes(b"a,b\n1,2\n")

    resp = await client.get(f"/api/media/{project.project_id}/{cid}/ab12cd34.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
