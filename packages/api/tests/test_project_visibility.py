"""Project visibility (public/private) + member whitelist tests."""
from __future__ import annotations

from sqlalchemy import func, select

from api.dependencies.auth import has_project_access, is_project_manager
from api.models.project import Project
from api.models.project_member import ProjectMember


async def _make_private(client, project, *, user: str = "test-user", with_user: str | None = None):
    """Flip a project to private and optionally whitelist with_user."""
    patch = await client.patch(
        f"/api/projects/{project.project_id}",
        json={"visibility": "private"},
    )
    assert patch.status_code == 200, patch.text
    if with_user is not None:
        add = await client.post(
            f"/api/projects/{project.project_id}/members",
            json={"user_id": with_user},
        )
        assert add.status_code == 201, add.text
    return patch


async def test_create_serializes_visibility(client):
    resp = await client.post("/api/projects", json={"display_name": "vis-create", "category": "software"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["visibility"] == "public"
    assert body["is_owner"] is True
    assert body["can_manage"] is True


async def test_private_project_denies_non_members(client, make_project, as_user):
    project = await make_project(slug="vis-private", created_by="test-user")
    await _make_private(client, project)

    async with as_user("other-user"):
        for path in (
            f"/api/projects/{project.project_id}",
            f"/api/projects/{project.project_id}/conversations",
            f"/api/projects/{project.project_id}/knowledge",
            f"/api/projects/{project.project_id}/mcp-servers",
            f"/api/projects/{project.project_id}/secrets",
        ):
            resp = await client.get(path)
            assert resp.status_code == 403, f"{path}: {resp.status_code}"
            assert resp.json()["detail"]["code"] == "PROJECT_PRIVATE", path


async def test_private_project_hidden_from_list_for_non_members(client, make_project, as_user):
    project = await make_project(slug="vis-hidden", created_by="test-user")
    await _make_private(client, project)

    # Non-member: the private project is completely absent from the list.
    async with as_user("other-user"):
        listing = await client.get("/api/projects")
        assert listing.status_code == 200
        ids = [p["project_id"] for p in listing.json()]
        assert project.project_id not in ids

    # Owner still sees it, with manage rights.
    listing = await client.get("/api/projects")
    item = next(p for p in listing.json() if p["project_id"] == project.project_id)
    assert item["visibility"] == "private"
    assert item["can_manage"] is True
    assert item["is_owner"] is True


async def test_member_can_access_and_manage_whitelist(client, make_project, as_user):
    project = await make_project(slug="vis-member", created_by="test-user")
    await _make_private(client, project, with_user="other-user")

    # Member: can access, can_manage=true, can add third user, cannot toggle visibility.
    async with as_user("other-user"):
        got = await client.get(f"/api/projects/{project.project_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["can_manage"] is True
        assert body["is_owner"] is False

        conv = await client.post(
            f"/api/projects/{project.project_id}/conversations", json={}
        )
        assert conv.status_code == 200, conv.text
        assert conv.json()["conversation_id"]

        add_third = await client.post(
            f"/api/projects/{project.project_id}/members",
            json={"user_id": "third-user"},
        )
        assert add_third.status_code == 201, add_third.text

        patch = await client.patch(
            f"/api/projects/{project.project_id}",
            json={"visibility": "public"},
        )
        assert patch.status_code == 403
        assert patch.json()["detail"]["code"] == "PROJECT_NOT_OWNER"

        members = await client.get(f"/api/projects/{project.project_id}/members")
        assert members.status_code == 200
        assert {m["user_id"] for m in members.json()} == {"other-user", "third-user"}
        # other-user added by the creator; third-user added by other-user
        assert {m["added_by"] for m in members.json()} == {"test-user", "other-user"}

    # Third user (added by a member) can now access.
    async with as_user("third-user"):
        got = await client.get(f"/api/projects/{project.project_id}")
        assert got.status_code == 200


async def test_member_removal_and_validation_errors(client, make_project, as_user, db):
    project = await make_project(slug="vis-remove", created_by="test-user")
    await _make_private(client, project, with_user="other-user")
    pid = project.project_id

    async with as_user("other-user"):
        # Member removes a fellow member → they lose access.
        await client.post(f"/api/projects/{pid}/members", json={"user_id": "third-user"})
        remove = await client.delete(f"/api/projects/{pid}/members/third-user")
        assert remove.status_code == 204, remove.text

        # Cannot remove the creator.
        remove_owner = await client.delete(f"/api/projects/{pid}/members/test-user")
        assert remove_owner.status_code == 400
        assert remove_owner.json()["detail"]["code"] == "MEMBER_IS_OWNER"

        # Cannot add the creator (implicit access already).
        add_owner = await client.post(f"/api/projects/{pid}/members", json={"user_id": "test-user"})
        assert add_owner.status_code == 400
        assert add_owner.json()["detail"]["code"] == "MEMBER_IS_OWNER"

        # Blank user_id → 400.
        blank = await client.post(f"/api/projects/{pid}/members", json={"user_id": "   "})
        assert blank.status_code == 400
        assert blank.json()["detail"]["code"] == "INVALID_USER_ID"

        # Removing a non-member → 404.
        missing = await client.delete(f"/api/projects/{pid}/members/ghost-user")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "MEMBER_NOT_FOUND"

        # Removing yourself revokes your own access.
        self_remove = await client.delete(f"/api/projects/{pid}/members/other-user")
        assert self_remove.status_code == 204

    # Removed member (never re-added): access denied.
    async with as_user("third-user"):
        assert (await client.get(f"/api/projects/{pid}")).status_code == 403

    # Re-adding (as creator) succeeds; duplicate add → 409.
    add = await client.post(f"/api/projects/{pid}/members", json={"user_id": "third-user"})
    assert add.status_code == 201, add.text
    dup = await client.post(f"/api/projects/{pid}/members", json={"user_id": "third-user"})
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "MEMBER_EXISTS"

    async with as_user("other-user"):
        assert (await client.get(f"/api/projects/{pid}")).status_code == 403

    count = (await db.execute(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == pid
        )
    )).scalar_one()
    assert count == 1  # only third-user remains


async def test_public_project_open_but_member_gate(client, make_project, as_user):
    project = await make_project(slug="vis-public", created_by="test-user")
    assert project.visibility == "public"

    async with as_user("other-user"):
        # Public project: anyone can access, but nobody may manage the whitelist.
        got = await client.get(f"/api/projects/{project.project_id}")
        assert got.status_code == 200
        assert got.json()["can_manage"] is False
        assert got.json()["is_owner"] is False

        add = await client.post(
            f"/api/projects/{project.project_id}/members",
            json={"user_id": "third-user"},
        )
        assert add.status_code == 403
        assert add.json()["detail"]["code"] == "PROJECT_NOT_MEMBER"

        members = await client.get(f"/api/projects/{project.project_id}/members")
        assert members.status_code == 403


async def test_removed_member_old_conversation_denied(client, make_project, as_user):
    project = await make_project(slug="vis-conv", created_by="test-user")
    await _make_private(client, project, with_user="other-user")

    async with as_user("other-user"):
        conv = await client.post(
            f"/api/projects/{project.project_id}/conversations", json={}
        )
        conv_id = conv.json()["conversation_id"]
        assert (await client.get(f"/api/conversations/{conv_id}/messages")).status_code == 200

    # Creator removes the member — their existing conversation must 403 too.
    await client.delete(f"/api/projects/{project.project_id}/members/other-user")

    async with as_user("other-user"):
        resp = await client.get(f"/api/conversations/{conv_id}/messages")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "PROJECT_PRIVATE"


async def test_deletion_cleans_members(client, make_project, db):
    project = await make_project(slug="vis-delete", created_by="test-user")
    await _make_private(client, project, with_user="other-user")
    pid = project.project_id

    resp = await client.request(
        "DELETE",
        f"/api/projects/{pid}",
        json={"confirmation": project.slug},
    )
    assert resp.status_code == 204, resp.text

    project_gone = (await db.execute(
        select(Project.project_id).where(Project.project_id == pid)
    )).first()
    assert project_gone is None
    members_left = (await db.execute(
        select(func.count()).select_from(ProjectMember).where(
            ProjectMember.project_id == pid
        )
    )).scalar_one()
    assert members_left == 0


async def test_access_helpers(db, make_project):
    """The WS handler reuses has_project_access — unit-test the shared logic."""
    public_p = await make_project(slug="help-public", created_by="test-user")
    private_p = await make_project(slug="help-private", created_by="test-user", visibility="private")

    assert await is_project_manager(db, public_p, "test-user") is True
    assert await has_project_access(db, public_p, "nobody") is True
    assert await has_project_access(db, private_p, "nobody") is False
    assert await is_project_manager(db, private_p, "nobody") is False

    db.add(ProjectMember(
        project_id=str(private_p.project_id),
        user_id="other-user",
        added_by="test-user",
    ))
    await db.commit()
    assert await is_project_manager(db, private_p, "other-user") is True
    assert await has_project_access(db, private_p, "other-user") is True
    assert await is_project_manager(db, private_p, "test-user") is True
    assert await has_project_access(db, private_p, "test-user") is True


async def test_ws_access_check_after_removal(client, make_project, as_user):
    """_check_project_access — the WS per-message gate — must fail closed once
    a member is removed (covers in-flight injections and user_selection_response
    secret saves, which never reach _handle_message's per-turn check)."""
    from api.websocket.handlers import _check_project_access

    project = await make_project(slug="vis-ws-gate", created_by="test-user")
    await _make_private(client, project, with_user="other-user")
    pid = project.project_id

    async with as_user("other-user"):
        conv = await client.post(f"/api/projects/{pid}/conversations", json={})
        conv_id = conv.json()["conversation_id"]
        # Member: full access via the WS gate.
        assert await _check_project_access(conv_id, "other-user") is True

    # Creator removes the member — the gate must deny their next WS message.
    await client.delete(f"/api/projects/{pid}/members/other-user")
    assert await _check_project_access(conv_id, "other-user") is False
    # An unknown conversation fails closed (the WS only ever calls the gate
    # with the socket owner's own conversation id).
    assert await _check_project_access("no-such-conversation", "other-user") is False


async def test_ws_access_check_public_open(client, make_project, as_user):
    """Public projects: any logged-in user with their own conversation passes
    the WS gate."""
    from api.websocket.handlers import _check_project_access

    project = await make_project(slug="vis-ws-public", created_by="test-user")
    conv = await client.post(f"/api/projects/{project.project_id}/conversations", json={})
    conv_id = conv.json()["conversation_id"]
    assert await _check_project_access(conv_id, "test-user") is True

    # Another user creates their own conversation on the public project.
    async with as_user("random-user"):
        conv2 = await client.post(f"/api/projects/{project.project_id}/conversations", json={})
        conv2_id = conv2.json()["conversation_id"]
        assert await _check_project_access(conv2_id, "random-user") is True
