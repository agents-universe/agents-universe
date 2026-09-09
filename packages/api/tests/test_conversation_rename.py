"""PATCH /api/conversations/{id} — manual title override.

Covers validation bounds, ownership/soft-delete guards, the deliberate
non-bump of updated_at (sidebar activity order), and the auto-title guard
that must never overwrite a title the user set.
"""
from __future__ import annotations

import uuid

import pytest

from api.models.conversation import Conversation
from api.websocket.handlers import _prepare_and_persist_user_message


async def _make_conversation(db, project, *, title=None, user_id="test-user", status="active"):
    conv = Conversation(
        conversation_id=f"c-{uuid.uuid4().hex[:8]}",
        project_id=project.project_id,
        user_id=user_id,
        title=title,
        status=status,
    )
    db.add(conv)
    await db.commit()
    return conv


async def _reload(db, conv: Conversation) -> Conversation:
    await db.refresh(conv)
    return conv


@pytest.mark.asyncio
async def test_rename_updates_title_and_returns_body(client, db, make_project):
    project = await make_project("ren-basic")
    conv = await _make_conversation(db, project, title="old")

    resp = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "new name"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"conversation_id": str(conv.conversation_id), "title": "new name"}

    await _reload(db, conv)
    assert conv.title == "new name"

    listed = await client.get(f"/api/projects/{project.project_id}/conversations")
    assert [r["title"] for r in listed.json()] == ["new name"]


@pytest.mark.asyncio
async def test_rename_trims_whitespace(client, db, make_project):
    project = await make_project("ren-trim")
    conv = await _make_conversation(db, project)

    resp = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "  padded  "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "padded"


@pytest.mark.asyncio
async def test_rename_rejects_blank_titles(client, db, make_project):
    project = await make_project("ren-blank")
    conv = await _make_conversation(db, project, title="keep me")

    for blank in ("", "   ", "\n\t "):
        resp = await client.patch(
            f"/api/conversations/{conv.conversation_id}", json={"title": blank}
        )
        assert resp.status_code == 422, f"{blank!r} -> {resp.status_code}"

    await _reload(db, conv)
    assert conv.title == "keep me"


@pytest.mark.asyncio
async def test_rename_enforces_title_length(client, db, make_project):
    project = await make_project("ren-length")
    conv = await _make_conversation(db, project)

    ok = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "x" * 255}
    )
    assert ok.status_code == 200, ok.text

    too_long = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "y" * 256}
    )
    assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_rename_does_not_bump_updated_at(client, db, make_project):
    """Sidebar order is COALESCE(updated_at, created_at) desc — renaming must
    not move the conversation to the top."""
    project = await make_project("ren-timestamp")
    conv = await _make_conversation(db, project)
    assert conv.updated_at is None

    resp = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "renamed"}
    )
    assert resp.status_code == 200, resp.text

    await _reload(db, conv)
    assert conv.updated_at is None


@pytest.mark.asyncio
async def test_rename_unicode_roundtrip(client, db, make_project):
    project = await make_project("ren-unicode")
    conv = await _make_conversation(db, project)

    title = "会话标题 · 排期 🎉"
    resp = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": title}
    )
    assert resp.status_code == 200, resp.text
    await _reload(db, conv)
    assert conv.title == title


@pytest.mark.asyncio
async def test_rename_unknown_conversation_404(client, make_project):
    await make_project("ren-unknown")
    resp = await client.patch("/api/conversations/does-not-exist", json={"title": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_soft_deleted_404(client, db, make_project):
    project = await make_project("ren-deleted")
    conv = await _make_conversation(db, project, title="gone")

    resp = await client.delete(f"/api/conversations/{conv.conversation_id}")
    assert resp.status_code == 200

    resp = await client.patch(
        f"/api/conversations/{conv.conversation_id}", json={"title": "resurrect"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_other_users_conversation_404(client, db, make_project, as_user):
    project = await make_project("ren-other")
    conv = await _make_conversation(db, project, user_id="other-user", title="theirs")

    async with as_user("test-user"):
        resp = await client.patch(
            f"/api/conversations/{conv.conversation_id}", json={"title": "stolen"}
        )
    assert resp.status_code == 404

    await _reload(db, conv)
    assert conv.title == "theirs"


@pytest.mark.asyncio
async def test_auto_title_does_not_clobber_manual_title(db, make_project):
    project = await make_project("ren-autotitle")
    conv = await _make_conversation(db, project, title="用户起的名字")
    from api.paths import resolve_project_fs_path

    fs_path = await resolve_project_fs_path(str(project.project_id), db)
    _, err = await _prepare_and_persist_user_message(
        db, str(conv.conversation_id), str(project.project_id), fs_path,
        "第一条消息不该覆盖标题", [], set_title=True,
    )
    assert err is None
    await _reload(db, conv)
    assert conv.title == "用户起的名字"


@pytest.mark.asyncio
async def test_auto_title_still_applies_to_empty_title(db, make_project):
    """ConversationCreate accepts title="" — the SQL guard must treat it as
    untitled (title IS NULL OR title = ''), not as a user-set title."""
    project = await make_project("ren-empty-autotitle")
    conv = await _make_conversation(db, project, title="")
    from api.paths import resolve_project_fs_path

    fs_path = await resolve_project_fs_path(str(project.project_id), db)
    _, err = await _prepare_and_persist_user_message(
        db, str(conv.conversation_id), str(project.project_id), fs_path,
        "自动标题应该生效", [], set_title=True,
    )
    assert err is None
    await _reload(db, conv)
    assert conv.title == "自动标题应该生效"
