"""Tests for the user-scoped email server integration CRUD + test endpoint.

Rows are per-user (Settings → Integrations → 邮件服务器); one user can
register several mailboxes under distinct slugs. Responses never carry the
password — only the last-4 key_hint.
"""
from __future__ import annotations

import uuid

import pytest


def _slug() -> str:
    """Unique per-test slug — the shared test DB persists across tests."""
    return f"mail-{uuid.uuid4().hex[:8]}"


def _body(**overrides):
    body = {
        "slug": _slug(),
        "display_name": "招聘收件箱",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "username": "hr@example.com",
        "password": "sup3r-s3cret-pass",
        "allow_private_network": False,
        "enabled": True,
    }
    body.update(overrides)
    return body


async def _create(client, **overrides):
    return await client.post("/api/email/servers", json=_body(**overrides))


async def test_create_list_update_delete(client):
    resp = await _create(client)
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["slug"]
    assert created["username"] == "hr@example.com"
    # Password never serialized — only a last-4 hint.
    assert "password" not in created
    assert "encrypted_password" not in created
    assert created["key_hint"].startswith("...")
    server_id = created["email_server_id"]

    resp = await client.get("/api/email/servers")
    assert resp.status_code == 200
    listing = {s["email_server_id"]: s for s in resp.json()}
    assert server_id in listing
    assert listing[server_id]["imap_host"] == "imap.example.com"

    # Update without a password keeps the stored one (no key_hint change).
    resp = await client.put(
        f"/api/email/servers/{server_id}",
        json=_body(slug=created["slug"], display_name="更新后的收件箱", password=None),
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["display_name"] == "更新后的收件箱"
    assert updated["key_hint"] == created["key_hint"]

    resp = await client.delete(f"/api/email/servers/{server_id}")
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/email/servers")
    assert server_id not in {s["email_server_id"] for s in resp.json()}


async def test_multiple_servers_per_user(client):
    """The integrations page registers several mailboxes, one row per slug."""
    first = await _create(client)
    second = await _create(client, display_name="第二个邮箱")
    assert first.status_code == 200 and second.status_code == 200
    slugs = {s["slug"] for s in (await client.get("/api/email/servers")).json()}
    assert first.json()["slug"] in slugs
    assert second.json()["slug"] in slugs
    # Clean up so later tests' list assertions stay independent.
    for sid in (first.json()["email_server_id"], second.json()["email_server_id"]):
        await client.delete(f"/api/email/servers/{sid}")


async def test_duplicate_slug_conflict(client):
    slug = _slug()
    assert (await _create(client, slug=slug)).status_code == 200
    resp = await _create(client, slug=slug)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "EMAIL_SERVER_EXISTS"


async def test_create_requires_password(client):
    resp = await _create(client, password=None)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "EMAIL_PASSWORD_REQUIRED"


async def test_rejects_bad_slug_and_security(client):
    resp = await _create(client, slug="Bad_Slug!")
    assert resp.status_code == 422
    resp = await _create(client, imap_security="tls2")
    assert resp.status_code == 422
    resp = await _create(client, imap_port=0)
    assert resp.status_code == 422


async def test_delete_missing_404(client):
    resp = await client.delete(f"/api/email/servers/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_update_missing_404(client):
    resp = await client.put(
        f"/api/email/servers/{uuid.uuid4()}", json=_body()
    )
    assert resp.status_code == 404


async def test_test_endpoint_reports_probe_results(client, monkeypatch):
    """The /test endpoint runs IMAP+SMTP probes; failures are redacted dicts."""
    from api.routers import email_servers as router_mod

    created = (await _create(client, password="hunter22-unique")).json()
    server_id = created["email_server_id"]

    calls = {}

    def _fake_imap(host, port, security, username, password):
        calls["imap"] = password
        return "IMAP login ok (OK)"

    def _fake_smtp(host, port, security, username, password):
        raise Exception("SMTP auth failed for password hunter22-unique")

    monkeypatch.setattr(router_mod, "_probe_imap", _fake_imap)
    monkeypatch.setattr(router_mod, "_probe_smtp", _fake_smtp)

    resp = await client.post(f"/api/email/servers/{server_id}/test")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["ok"] is False
    assert detail["imap"]["ok"] is True
    assert detail["smtp"]["ok"] is False
    # The submitted password echoed by the fake SMTP error must be scrubbed.
    assert "hunter22-unique" not in resp.text
    assert "[REDACTED]" in detail["smtp"]["error"]
    await client.delete(f"/api/email/servers/{server_id}")


async def test_test_endpoint_all_green(client, monkeypatch):
    from api.routers import email_servers as router_mod

    created = (await _create(client)).json()
    server_id = created["email_server_id"]
    monkeypatch.setattr(router_mod, "_probe_imap", lambda *a: "IMAP login ok")
    monkeypatch.setattr(router_mod, "_probe_smtp", lambda *a: "SMTP auth ok")

    resp = await client.post(f"/api/email/servers/{server_id}/test")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["ok"] is True
    assert detail["imap"]["ok"] is True and detail["smtp"]["ok"] is True
    await client.delete(f"/api/email/servers/{server_id}")


async def test_scoped_to_current_user(client, as_user):
    """Another user's servers are invisible and untouchable."""
    created = (await _create(client)).json()
    server_id = created["email_server_id"]

    async with as_user("other-user"):
        listing = (await client.get("/api/email/servers")).json()
        assert server_id not in {s["email_server_id"] for s in listing}
        resp = await client.delete(f"/api/email/servers/{server_id}")
        assert resp.status_code == 404
