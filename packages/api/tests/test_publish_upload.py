"""Publish API attachment upload tests — POST /api/p/{publish_id}/upload.

Two-step attachment flow: upload (API key, multipart) returns a media ref
bound to the publish conversation, then RunRequest.attachments carries it
into the run. The kernel's _validate_attachment_url stays the single deep
validation gate — these tests pin the router-side contract (auth, size,
quota, ref shape) and the round-trip into the turn.
"""
from __future__ import annotations

import io
import re
import uuid

from PIL import Image
from sqlalchemy import select

from api.models.conversation import Conversation
from api.models.publish import AgentPublish, PublishKey
from api.models.user import UserModelConfig
from api.services.publish import generate_publish_key, hash_publish_key


# ── helpers (local copies — test modules do not import each other) ──────────


async def _make_publish(db, make_project, *, user_id="test-user", agent_slug=None):
    agent_slug = agent_slug or f"t-agent-{uuid.uuid4().hex[:8]}"
    project = await make_project(created_by=user_id)
    from api.models.agent import Agent
    db.add(Agent(slug=agent_slug, display_name=agent_slug, project_id=None))
    cfg = UserModelConfig(
        user_id=user_id,
        provider="openai",
        model_id="p-model",
        encrypted_key="not-a-real-key",
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    publish = AgentPublish(
        owner_id=user_id,
        agent_slug=agent_slug,
        project_id=str(project.project_id),
        model_config_id=cfg.config_id,
        title="Test publish",
    )
    db.add(publish)
    await db.commit()
    await db.refresh(publish)
    return publish, cfg


def _mk_key(publish_id: str):
    plain = generate_publish_key(publish_id)
    return plain, hash_publish_key(plain)


def _auth(plain: str) -> dict:
    return {"Authorization": f"Bearer {plain}"}


async def _publish_with_key(db, make_project, **kw):
    publish, cfg = await _make_publish(db, make_project, **kw)
    plain, digest = _mk_key(publish.publish_id)
    db.add(PublishKey(publish_id=publish.publish_id, key_hash=digest, key_hint="...z"))
    await db.commit()
    return publish, plain


def _png_bytes(width: int = 16, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


async def _set_project_visibility(db, publish, visibility: str):
    from api.models.project import Project
    await db.execute(
        Project.__table__.update()
        .where(Project.project_id == publish.project_id)
        .values(visibility=visibility)
    )
    await db.commit()


# ── auth / gating ───────────────────────────────────────────────────────────


async def test_upload_requires_key(client, db, make_project):
    publish, plain = await _publish_with_key(db, make_project)
    other, other_plain = await _publish_with_key(db, make_project)

    # No key -> 401
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "PUBLISH_API_KEY_REQUIRED"

    # Another publish's key -> 401 mismatch
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        headers=_auth(other_plain),
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "PUBLISH_API_KEY_MISMATCH"
    assert other.publish_id != publish.publish_id


async def test_upload_disabled_or_private(client, db, make_project):
    publish, plain = await _publish_with_key(db, make_project)

    publish.api_enabled = False
    await db.commit()
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        headers=_auth(plain),
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PUBLISH_API_DISABLED"

    publish.api_enabled = True
    await db.commit()
    await _set_project_visibility(db, publish, "private")
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        headers=_auth(plain),
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PROJECT_PRIVATE"


# ── ref shape / round-trip ──────────────────────────────────────────────────


async def test_upload_returns_media_ref_for_publish_conversation(
    client, db, make_project
):
    """The ref URL binds to the (publish, thread) conversation, and the
    upload is served back through the ordinary media GET."""
    publish, plain = await _publish_with_key(db, make_project)

    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        params={"thread_id": "t1"},
        headers=_auth(plain),
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 200
    record = r.json()
    assert record["name"] == "notes.txt"
    assert record["media_type"] == "text/plain"
    assert record["size"] == 11
    prefix = f"/api/media/{publish.project_id}/"
    assert record["url"].startswith(prefix)
    stored_name = record["url"].rsplit("/", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{32}\.txt", stored_name)

    # The conversation behind the ref is the publish-scoped one for thread t1.
    cid = record["url"][len(prefix):].rsplit("/", 1)[0]
    conv = (
        await db.execute(select(Conversation).where(Conversation.conversation_id == cid))
    ).scalar_one()
    assert conv.source == "publish"
    assert conv.publish_id == str(publish.publish_id)
    assert conv.thread_id == "t1"
    assert conv.user_id == publish.owner_id
    assert str(conv.project_id) == publish.project_id

    # GET roundtrip serves the bytes (SSO auth is bypassed in tests).
    resp = await client.get(record["url"])
    assert resp.status_code == 200
    assert resp.content == b"hello world"


async def test_upload_image_carries_dimensions(client, db, make_project):
    publish, plain = await _publish_with_key(db, make_project)
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        headers=_auth(plain),
        files={"file": ("shot.png", _png_bytes(16, 8), "image/png")},
    )
    assert r.status_code == 200
    record = r.json()
    assert record["media_type"] == "image/png"
    assert record["width"] == 16 and record["height"] == 8


async def test_upload_then_run_passes_attachments(
    client, db, make_project, monkeypatch
):
    """Upload ref -> RunRequest.attachments -> msg['attachments'], and the
    kernel's deep validator accepts it against the captured conversation."""
    from pathlib import Path

    from api.services.agent_turn import _validate_attachment_url

    publish, plain = await _publish_with_key(db, make_project)
    up = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        params={"thread_id": "attach-thread"},
        headers=_auth(plain),
        files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert up.status_code == 200
    record = up.json()

    captured = {}

    async def _fake_run_turn(conversation_id, ws, msg, user_id, *, transport=None, interactive=True, actor_user_id=None):
        captured["conversation_id"] = conversation_id
        captured["attachments"] = msg.get("attachments")
        await transport.send(conversation_id, {"type": "stream_delta", "delta": "ok"})
        await transport.send(
            conversation_id,
            {"type": "stream_end", "message_id": "m1", "total_tokens": 0},
        )

    monkeypatch.setattr("api.services.agent_turn.run_turn", _fake_run_turn)

    # The record embeds extra keys (id, …) — pydantic drops them on parse.
    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers=_auth(plain),
        json={
            "message": "check this invoice",
            "thread_id": "attach-thread",
            "attachments": [record],
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    atts = captured["attachments"]
    assert atts and atts[0]["url"] == record["url"]
    assert atts[0]["name"] == "invoice.pdf"

    # Deep gate: same project + conversation, file found in the store.
    media_dir = Path("nonexistent") / ".tmp" / "media" / captured["conversation_id"]
    resolved = _validate_attachment_url(
        atts[0], publish.project_id, captured["conversation_id"], media_dir
    )
    assert resolved is not None


# ── size / quota ────────────────────────────────────────────────────────────


async def test_upload_too_large(client, db, make_project, monkeypatch):
    from api.config import Settings

    monkeypatch.setattr(
        "api.routers.publish.get_settings", lambda: Settings(max_upload_size_mb=1)
    )
    publish, plain = await _publish_with_key(db, make_project)
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        headers=_auth(plain),
        files={"file": ("big.bin", b"x" * (1024 * 1024 + 100), "application/octet-stream")},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "FILE_TOO_LARGE"


async def test_oversized_content_length_rejected_at_asgi_edge(client, db, make_project):
    """The middleware guard matches /api/p/*/upload on the header alone —
    before FastAPI spools the multipart body to temp disk."""
    publish, plain = await _publish_with_key(db, make_project)
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        content=b"x",
        headers={**_auth(plain), "content-length": str(64 * 1024 * 1024)},
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "FILE_TOO_LARGE"


async def test_upload_conversation_quota(client, db, make_project, monkeypatch):
    from api.routers.media import drop_uploads

    publish, plain = await _publish_with_key(db, make_project)
    r1 = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        params={"thread_id": "quota"},
        headers=_auth(plain),
        files={"file": ("a.txt", b"first", "text/plain")},
    )
    assert r1.status_code == 200
    cid = r1.json()["url"].rsplit("/", 2)[-2]

    monkeypatch.setattr("api.routers.media._MAX_CONVERSATION_UPLOAD_BYTES", 1)
    try:
        r2 = await client.post(
            f"/api/p/{publish.publish_id}/upload",
            params={"thread_id": "quota"},
            headers=_auth(plain),
            files={"file": ("b.txt", b"second", "text/plain")},
        )
        assert r2.status_code == 413
        assert r2.json()["detail"]["code"] == "CONVERSATION_UPLOAD_QUOTA"
    finally:
        drop_uploads(cid)


async def test_upload_never_written_to_disk(client, db, make_project):
    from sqlalchemy import select

    from api.models.project import Project
    from api.paths import PROJECTS_ROOT

    publish, plain = await _publish_with_key(db, make_project)
    r = await client.post(
        f"/api/p/{publish.publish_id}/upload",
        params={"thread_id": "nodisk"},
        headers=_auth(plain),
        files={"file": ("a.txt", b"memory only", "text/plain")},
    )
    assert r.status_code == 200
    cid = r.json()["url"].rsplit("/", 2)[-2]
    # publish.project_id is a UUID — resolve the workspace slug via the row.
    proj = (
        await db.execute(select(Project).where(Project.project_id == publish.project_id))
    ).scalar_one()
    media_dir = PROJECTS_ROOT / proj.slug / ".tmp" / "media" / cid
    assert not media_dir.exists() or not any(media_dir.iterdir())


# ── RunRequest.attachments validation ───────────────────────────────────────


async def test_run_rejects_foreign_attachment_url(client, db, make_project):
    publish, plain = await _publish_with_key(db, make_project)
    other, _ = await _make_publish(db, make_project)

    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers=_auth(plain),
        json={
            "message": "hi",
            "attachments": [
                {"url": f"/api/media/{other.project_id}/some-conv/aaaa.txt", "name": "a.txt"}
            ],
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PUBLISH_ATTACHMENT_INVALID"


async def test_run_attachment_shape_validated_by_pydantic(client, db, make_project):
    publish, plain = await _publish_with_key(db, make_project)

    # Missing url -> 422
    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers=_auth(plain),
        json={"message": "hi", "attachments": [{"name": "a.txt"}]},
    )
    assert r.status_code == 422

    # 11 refs -> 422 (≤10, same bound as the WS path)
    refs = [
        {"url": f"/api/media/{publish.project_id}/c/{i}.txt", "name": f"{i}.txt"}
        for i in range(11)
    ]
    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers=_auth(plain),
        json={"message": "hi", "attachments": refs},
    )
    assert r.status_code == 422
