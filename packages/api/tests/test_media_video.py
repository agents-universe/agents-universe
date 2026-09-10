"""Video media serving — .webm/.mp4 play inline instead of downloading.

browser_playwright's record_stop writes a Playwright .webm into the
conversation media dir; without a video/webm mapping the browser gets
application/octet-stream and silently downloads the evidence instead of
playing it. The upload endpoint shares the same suffix table, so an
uploaded clip must carry the video type on the record too.
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


async def test_serve_media_disk_webm_inline(client, db, make_project):
    """A recording on disk is served as video/webm with 200."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "recording_ab12cd34.webm").write_bytes(b"\x1a\x45\xdf\xa3fake-webm")
    (media_dir / "recording_ab12cd34.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp4")

    for fname in ("recording_ab12cd34.webm", "recording_ab12cd34.mp4"):
        resp = await client.get(f"/api/media/{project.project_id}/{cid}/{fname}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == (
            "video/webm" if fname.endswith(".webm") else "video/mp4"
        )
        # Video plays inline — never a forced download (unlike .html).
        assert "content-disposition" not in {k.lower() for k in resp.headers}


async def test_serve_media_disk_webm_owner_only(client, db, make_project):
    """Recordings follow the same owner-only rule as the rest of the media."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    media_dir = PROJECTS_ROOT / project.slug / ".tmp" / "media" / cid
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "recording_ab12cd34.webm").write_bytes(b"\x1a\x45\xdf\xa3fake-webm")
    url = f"/api/media/{project.project_id}/{cid}/recording_ab12cd34.webm"

    resp = await client.get(url)
    assert resp.status_code == 200

    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == cid)
        .values(user_id="other-user")
    )
    await db.commit()
    resp = await client.get(url)
    assert resp.status_code == 404


async def test_upload_webm_served_as_video(client, make_project):
    """An uploaded .webm keeps video/webm on the record and the GET response."""
    project = await make_project()
    cid = await _make_conversation(client, str(project.project_id))

    resp = await client.post(
        f"/api/media/{project.project_id}/{cid}",
        files={"file": ("clip.webm", b"\x1a\x45\xdf\xa3fake-webm", "video/webm")},
    )
    assert resp.status_code == 200
    record = resp.json()
    assert record["media_type"] == "video/webm"

    resp = await client.get(record["url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/webm"
    assert resp.content == b"\x1a\x45\xdf\xa3fake-webm"
