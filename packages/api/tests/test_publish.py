"""Agent-as-a-Service publish tests.

Covers the management CRUD + API-key lifecycle (SSO), the public SSE stream
(API-key auth, bounded concurrency, abort) and the non-interactive turn path.
The LLM is replaced with a spy Agent so no external provider is contacted.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from api.models.conversation import Conversation
from api.models.publish import AgentPublish, PublishKey
from api.models.user import UserModelConfig
from api.services.publish import (
    authenticate_publish_key,
    generate_publish_key,
    hash_publish_key,
)


# ── helpers ────────────────────────────────────────────────────────────────


async def _make_publish(db, make_project, *, user_id="test-user", agent_slug=None, model_id="p-model", with_agent=True):
    agent_slug = agent_slug or f"t-agent-{uuid.uuid4().hex[:8]}"
    project = await make_project(created_by=user_id)
    if with_agent:
        from api.models.agent import Agent
        db.add(Agent(
            slug=agent_slug,
            display_name=agent_slug,
            project_id=None,  # global agent
        ))
        await db.commit()
    cfg = UserModelConfig(
        user_id=user_id,
        provider="openai",
        model_id=model_id,
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


# ── key hashing / auth helpers ─────────────────────────────────────────────


async def test_generate_hash_roundtrip(db, make_project):
    """The stored hash authenticates the plaintext, and never the other way."""
    # publish_keys.publish_id is an FK to agent_publishes — create the parent
    # row or PostgreSQL rejects the insert (SQLite, with FK enforcement off by
    # default, would not). The key embeds the real publish id as its prefix.
    publish, _ = await _make_publish(db, make_project)
    plain, digest = _mk_key(publish.publish_id)
    assert plain.startswith(f"pua_{publish.publish_id}_")
    assert hash_publish_key(plain) == digest
    # Store a row so authenticate resolves it.
    key = PublishKey(publish_id=publish.publish_id, name="k", key_hash=digest, key_hint="..." + plain[-4:])
    db.add(key)
    await db.commit()

    found = await authenticate_publish_key(db, plain)
    assert found is not None and found[0] == publish.publish_id
    # A wrong key must not authenticate (compare_digest, constant time).
    assert await authenticate_publish_key(db, "pua_p1_" + "x" * 43) is None
    # Non-prefixed / malformed keys never reach the DB scan.
    assert await authenticate_publish_key(db, "pua_p1_short") is None
    assert await authenticate_publish_key(db, "not-a-key") is None


async def test_key_hash_not_plaintext_in_db(db, make_project):
    """The DB row stores only the SHA-256, never the raw key."""
    publish, _ = await _make_publish(db, make_project)
    plain = generate_publish_key(publish.publish_id)
    digest = hash_publish_key(plain)
    row = PublishKey(publish_id=publish.publish_id, key_hash=digest, key_hint="..." + plain[-4:])
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.key_hash == digest
    assert plain not in (row.key_hash, row.key_hint or "")
    assert row.key_hint == "..." + plain[-4:]


# ── management CRUD (SSO) ──────────────────────────────────────────────────


async def test_create_publish_requires_owned_model_config(client, db, make_project, as_user):
    project = await make_project(created_by="test-user")
    from api.models.agent import Agent
    slug = f"t-agent-{uuid.uuid4().hex[:8]}"
    db.add(Agent(slug=slug, display_name=slug, project_id=None))
    # Another user's model config must be rejected.
    other_cfg = UserModelConfig(user_id="someone-else", provider="openai", model_id="x", encrypted_key="k")
    db.add(other_cfg)
    await db.commit()

    async with as_user("test-user"):
        r = await client.post("/api/publishes", json={
            "agent_slug": slug,
            "project_id": str(project.project_id),
            "model_config_id": other_cfg.config_id,
        })
    assert r.status_code == 400  # model config not owned by publisher

    async with as_user("other-user"):
        r = await client.post("/api/publishes", json={
            "agent_slug": slug,
            "project_id": str(project.project_id),
            "model_config_id": other_cfg.config_id,
        })
    # other-user is not a member of the project -> forbidden
    assert r.status_code == 403


async def test_create_and_list_publish(client, db, make_project, as_user):
    from api.models.agent import Agent
    project = await make_project(created_by="test-user")
    cfg = UserModelConfig(user_id="test-user", provider="openai", model_id="gpt-x", encrypted_key="k")
    db.add(cfg)
    await db.commit()

    slug = f"t-agent-{uuid.uuid4().hex[:8]}"
    db.add(Agent(slug=slug, display_name=slug, project_id=None))
    await db.commit()
    async with as_user("test-user"):
        r = await client.post("/api/publishes", json={
            "agent_slug": slug,
            "project_id": str(project.project_id),
            "model_config_id": cfg.config_id,
            "title": "My publish",
        })
    assert r.status_code == 201, r.text
    body = r.json()
    pubid = body["publish_id"]
    assert body["agent_slug"] == slug
    assert body["model_config_id"] == cfg.config_id

    async with as_user("test-user"):
        r = await client.get("/api/publishes")
    assert r.status_code == 200
    assert any(p["publish_id"] == pubid for p in r.json())


async def test_publish_key_plaintext_shown_once(client, db, make_project, as_user):
    publish, _ = await _make_publish(db, make_project)
    async with as_user("test-user"):
        r = await client.post(f"/api/publishes/{publish.publish_id}/keys", json={"name": "prod"})
    assert r.status_code == 201, r.text
    body = r.json()
    plain = body["key"]
    assert plain.startswith("pua_")

    # Listing keys must never include the plaintext.
    async with as_user("test-user"):
        r = await client.get(f"/api/publishes/{publish.publish_id}/keys")
    assert r.status_code == 200
    listed = r.json()
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert listed[0]["key_hint"] == "..." + plain[-4:]

    # Revoke: subsequent API calls with the key fail.
    async with as_user("test-user"):
        r = await client.delete(f"/api/publishes/{publish.publish_id}/keys/{listed[0]['key_id']}")
    assert r.status_code == 204
    from api.services.publish import authenticate_publish_key
    assert await authenticate_publish_key(db, plain) is None


async def test_publish_update_toggle(client, db, make_project, as_user):
    publish, _ = await _make_publish(db, make_project)
    async with as_user("test-user"):
        r = await client.patch(f"/api/publishes/{publish.publish_id}", json={"api_enabled": False})
    assert r.status_code == 200
    await db.refresh(publish)
    assert publish.api_enabled is False


# ── public stream (API key) ────────────────────────────────────────────────


async def test_stream_requires_valid_key(client, db, make_project):
    publish, _ = await _make_publish(db, make_project)
    # No key -> 401
    r = await client.post(f"/api/p/{publish.publish_id}/stream", json={"message": "hi"})
    assert r.status_code == 401
    # Wrong key -> 401
    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers={"Authorization": "Bearer pua_wrong_xyz"},
        json={"message": "hi"},
    )
    assert r.status_code == 401


async def test_stream_disabled_publish(client, db, make_project, as_user):
    publish, _ = await _make_publish(db, make_project)
    publish.api_enabled = False
    await db.commit()
    plain, _ = _mk_key(publish.publish_id)
    row = PublishKey(publish_id=publish.publish_id, key_hash=hash_publish_key(plain), key_hint="...abc")
    db.add(row)
    await db.commit()
    r = await client.post(
        f"/api/p/{publish.publish_id}/stream",
        headers={"Authorization": f"Bearer {plain}"},
        json={"message": "hi"},
    )
    assert r.status_code == 403


async def test_stream_creates_publish_conversation_and_runs(
    client, db, make_project, monkeypatch
):
    """A turn runs against a source='publish' conversation owned by the publisher."""
    publish, _ = await _make_publish(db, make_project)
    plain, _ = _mk_key(publish.publish_id)
    db.add(PublishKey(publish_id=publish.publish_id, key_hash=hash_publish_key(plain), key_hint="...z"))
    await db.commit()

    # Spy the turn kernel so no LLM is contacted.
    captured = {}

    async def _fake_run_turn(conversation_id, ws, msg, user_id, *, transport=None, interactive=True, actor_user_id=None):
        captured["conversation_id"] = conversation_id
        captured["user_id"] = user_id
        captured["actor_user_id"] = actor_user_id
        captured["interactive"] = interactive
        captured["fixed_config_id"] = msg.get("fixed_config_id")
        # Emit a terminal event so the SSE stream closes.
        await transport.send(conversation_id, {"type": "stream_delta", "delta": "hi"})
        await transport.send(conversation_id, {"type": "stream_end", "message_id": "m1", "total_tokens": 0})

    # The router imports run_turn from the agent_turn module — patch there so
    # the router's lookup resolves the spy.
    monkeypatch.setattr("api.services.agent_turn.run_turn", _fake_run_turn)

    async with client.stream(
        "POST", f"/api/p/{publish.publish_id}/stream",
        headers={"Authorization": f"Bearer {plain}"},
        json={"message": "hello world"},
    ) as resp:
        assert resp.status_code == 200
        body = ""
        async for line in resp.aiter_lines():
            body += line
        # One data frame: stream_delta + stream_end should both appear.
        assert "stream_delta" in body
        assert "stream_end" in body

    # The kernel ran as the publisher with their bound model and interactive off.
    assert captured["user_id"] == publish.owner_id
    assert captured["actor_user_id"] == publish.owner_id
    assert captured["interactive"] is False
    assert captured["fixed_config_id"] == publish.model_config_id

    # The conversation is marked publish-owned.
    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == captured["conversation_id"])
    )
    conv = result.scalar_one()
    assert conv.source == "publish"
    assert conv.user_id == publish.owner_id


async def test_abort_endpoint(client, db, make_project):
    publish, _ = await _make_publish(db, make_project)
    plain, _ = _mk_key(publish.publish_id)
    db.add(PublishKey(publish_id=publish.publish_id, key_hash=hash_publish_key(plain), key_hint="...q"))
    await db.commit()
    r = await client.post(
        f"/api/p/{publish.publish_id}/abort",
        headers={"X-API-Key": plain},
    )
    assert r.status_code == 200


async def test_publish_conversations_hidden_from_sidebar(client, db, make_project, as_user):
    """source='publish' conversations never appear in the ordinary lists."""
    publish, _ = await _make_publish(db, make_project)
    from api.services.publish import get_or_create_publish_conversation
    conv_id = await get_or_create_publish_conversation(db, publish)

    async with as_user("test-user"):
        r = await client.get(f"/api/projects/{publish.project_id}/conversations")
        r2 = await client.get(f"/api/projects/{publish.project_id}/conversations/latest")
    assert r.status_code == 200
    ids = [c["conversation_id"] for c in r.json()]
    assert conv_id not in ids
    assert (r2.json() or {}).get("conversation_id") != conv_id


# ── SSO embedded page (viewer session) ─────────────────────────────────────


def _make_viewer_token(publish_id: str, viewer_id: str) -> str:
    from api.services.publish import publish_viewer_token
    return publish_viewer_token(publish_id, viewer_id)


async def test_page_tokenless_first_call(client, db, make_project, as_user):
    """A logged-in viewer opens /p/<id> without any token: the page endpoint
    issues a viewer-bound token and the payload in one cookie-authenticated
    call (this is the page's very first request)."""
    publish, _ = await _make_publish(db, make_project)

    async with as_user("test-user"):
        r = await client.get(f"/api/p/{publish.publish_id}/page")
    assert r.status_code == 200
    data = r.json()
    assert data["publish_id"] == str(publish.publish_id)
    assert data["conversation_id"]
    assert data["token"]
    # The issued token opens the /session paths for the same viewer.
    async with as_user("test-user"):
        r2 = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": data["token"]},
        )
    assert r2.status_code == 200

    # A different viewer cannot reuse the issued token.
    async with as_user("other-user"):
        r3 = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": data["token"]},
        )
    assert r3.status_code == 404


async def test_page_tokenless_disabled_hidden(client, db, make_project, as_user):
    """page_enabled=False makes the page endpoint 404 (indistinguishable from
    a nonexistent publish — no existence oracle for disabled pages)."""
    publish, _ = await _make_publish(db, make_project)
    publish.page_enabled = False
    await db.commit()

    async with as_user("test-user"):
        r = await client.get(f"/api/p/{publish.publish_id}/page")
    assert r.status_code == 404


async def test_session_requires_valid_token(client, db, make_project, as_user):
    """The page payload's token is bound to (publish, viewer); a wrong token
    or a different user cannot open the conversation."""
    publish, _ = await _make_publish(db, make_project)

    async with as_user("test-user"):
        # Missing token -> 404 (fail closed)
        r = await client.get(f"/api/p/{publish.publish_id}/session")
        assert r.status_code == 422  # query param required by schema

        r = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": "wrong-token"},
        )
        assert r.status_code == 404
        r2 = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": _make_viewer_token(str(publish.publish_id), "other-user")},
        )
        assert r2.status_code == 404


async def test_session_gets_or_creates_conversation(client, db, make_project, as_user):
    """A page load creates the shared publish conversation (owner-owned)."""
    publish, _ = await _make_publish(db, make_project)

    async with as_user("test-user"):
        r = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["conversation_id"]
    assert data["publish_id"] == str(publish.publish_id)
    assert data["token"]  # a fresh viewer token for the socket path

    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == data["conversation_id"])
    )
    conv = result.scalar_one()
    assert conv.source == "publish"
    assert conv.user_id == publish.owner_id

    # Idempotent: a second load reuses the same conversation.
    async with as_user("test-user"):
        r2 = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
    assert r2.json()["conversation_id"] == data["conversation_id"]


async def test_session_page_disabled_publish(client, db, make_project, as_user):
    """page_enabled=False hides the page from every viewer."""
    publish, _ = await _make_publish(db, make_project)
    publish.page_enabled = False
    await db.commit()

    async with as_user("test-user"):
        r = await client.get(
            f"/api/p/{publish.publish_id}/session",
            params={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
    assert r.status_code == 404


async def test_session_messages_and_latest_run(client, db, make_project, as_user):
    """History + run status read back for a viewer of the shared conversation."""
    publish, _ = await _make_publish(db, make_project)
    from api.services.publish import get_or_create_publish_conversation
    conv_id = await get_or_create_publish_conversation(db, publish)

    # Seed one message + run row directly (the turn kernel persists these).
    from api.models.conversation import Message as DbMessage
    from api.models.conversation_run import ConversationRun
    from api.models._compat import new_uuid
    db.add(DbMessage(
        message_id=new_uuid(),
        conversation_id=conv_id,
        role="user",
        content="hello",
        sequence_num=0,
    ))
    run = ConversationRun(
        conversation_id=conv_id,
        status="completed",
        tokens_used=0,
    )
    db.add(run)
    await db.commit()

    async with as_user("test-user"):
        r = await client.get(
            f"/api/p/{publish.publish_id}/session/messages",
            params={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
        r2 = await client.get(
            f"/api/p/{publish.publish_id}/session/runs/latest",
            params={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
    assert r.status_code == 200
    msgs = r.json()
    assert any(m["content"] == "hello" for m in msgs)
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"


async def test_session_run_streams_under_publisher(
    client, db, make_project, as_user, monkeypatch
):
    """A viewer's message runs non-interactively as the publisher, pinned to
    the publisher's bound model config."""
    publish, _ = await _make_publish(db, make_project)
    token = _make_viewer_token(str(publish.publish_id), "test-user")
    captured = {}

    async def _fake_run_turn(conversation_id, ws, msg, user_id, *, transport=None, interactive=True, actor_user_id=None):
        captured["user_id"] = user_id
        captured["actor_user_id"] = actor_user_id
        captured["interactive"] = interactive
        captured["fixed_config_id"] = msg.get("fixed_config_id")
        await transport.send(conversation_id, {"type": "stream_delta", "delta": "pong"})
        await transport.send(conversation_id, {"type": "stream_end", "message_id": "m1", "total_tokens": 0})

    monkeypatch.setattr("api.services.agent_turn.run_turn", _fake_run_turn)

    async with as_user("test-user"):
        async with client.stream(
            "POST", f"/api/p/{publish.publish_id}/session/run",
            json={"token": token, "message": "ping"},
        ) as resp:
            assert resp.status_code == 200
            body = ""
            async for line in resp.aiter_lines():
                body += line
            assert "stream_delta" in body
            assert "stream_end" in body

    assert captured["user_id"] == publish.owner_id
    assert captured["actor_user_id"] == publish.owner_id
    assert captured["interactive"] is False
    assert captured["fixed_config_id"] == publish.model_config_id


async def test_session_run_bad_token(client, db, make_project, as_user):
    publish, _ = await _make_publish(db, make_project)
    async with as_user("test-user"):
        r = await client.post(
            f"/api/p/{publish.publish_id}/session/run",
            json={"token": "nope", "message": "hi"},
        )
    # Schema rejects a short token before auth runs; use a full-length wrong
    # token to exercise the auth gate itself.
    assert r.status_code == 422
    async with as_user("test-user"):
        r2 = await client.post(
            f"/api/p/{publish.publish_id}/session/run",
            json={"token": "x" * 40, "message": "hi"},
        )
    assert r2.status_code == 404


async def test_session_run_creates_abort_event(
    client, db, make_project, as_user, monkeypatch
):
    """The SSE stream path must register an abort event for the conversation.

    Abort events are normally created on WS ``connect()``; publish streams
    never open a socket, so without ensure_abort_event run_turn's abort
    watcher is never created and "stop" is a silent no-op. Hitting the stream
    endpoint must leave a settable event behind.
    """
    publish, _ = await _make_publish(db, make_project)
    token = _make_viewer_token(str(publish.publish_id), "test-user")

    async def _fake_run_turn(conversation_id, ws, msg, user_id, *, transport=None, interactive=True, actor_user_id=None):
        await transport.send(conversation_id, {"type": "stream_delta", "delta": "hi"})
        await transport.send(conversation_id, {"type": "stream_end", "message_id": "m1", "total_tokens": 0})

    monkeypatch.setattr("api.services.agent_turn.run_turn", _fake_run_turn)

    from api.services.publish import get_or_create_publish_conversation
    from api.websocket.manager import manager

    conv_id = await get_or_create_publish_conversation(db, publish)
    # No WS was ever connected, so no event exists yet.
    assert manager.get_abort_event(conv_id) is None

    async with as_user("test-user"):
        async with client.stream(
            "POST", f"/api/p/{publish.publish_id}/session/run",
            json={"token": token, "message": "ping"},
        ) as resp:
            assert resp.status_code == 200
            async for _ in resp.aiter_lines():
                pass

    # The stream registered the event; abort now actually fires.
    event = manager.get_abort_event(conv_id)
    assert event is not None
    assert not event.is_set()
    manager.signal_abort(conv_id)
    assert event.is_set()


async def test_session_abort(client, db, make_project, as_user, monkeypatch):
    """A viewer can abort the running turn of the shared publish conversation."""
    publish, _ = await _make_publish(db, make_project)
    from api.services.publish import get_or_create_publish_conversation
    conv_id = await get_or_create_publish_conversation(db, publish)

    signalled = []
    import api.websocket.manager as wm
    monkeypatch.setattr(
        wm.manager, "signal_abort",
        lambda conversation_id: signalled.append(conversation_id),
    )

    async with as_user("test-user"):
        r = await client.post(
            f"/api/p/{publish.publish_id}/session/abort",
            json={"token": _make_viewer_token(str(publish.publish_id), "test-user")},
        )
    assert r.status_code == 200
    assert r.json()["aborted"] is True
    assert signalled == [conv_id]

    # A wrong token cannot abort.
    async with as_user("test-user"):
        r2 = await client.post(
            f"/api/p/{publish.publish_id}/session/abort",
            json={"token": "x" * 40},
        )
    assert r2.status_code == 404
    assert signalled == [conv_id]
