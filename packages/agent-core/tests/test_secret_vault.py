"""Tests for the secret_vault tool (user key vault management)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.tools.base import ToolContext
from agent_core.tools.secret_vault import SecretVaultTool


class FakeSession:
    """Records request_user_selection calls; returns the configured result."""

    def __init__(self, result="secret_saved"):
        self.result = result
        self.calls = []

    async def request_user_selection(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeDb:
    """In-memory stand-in for an async SQLAlchemy session."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        result = MagicMock()
        result.fetchall.return_value = self.rows
        return result

    async def commit(self):
        self.commits += 1


def make_context(session=None, db=None) -> ToolContext:
    return ToolContext(
        project_id="proj",
        project_fs_path="/tmp/proj",
        conversation_id="conv",
        user_id="user-1",
        db_session=db,
        session=session,
    )


@pytest.mark.asyncio
async def test_list_returns_metadata_without_plaintext():
    db = FakeDb(rows=[
        SimpleNamespace(
            service_key="myapi:token",
            display_name="My API",
            key_hint="a****xyz",
            base_url="https://api.example.com",
        ),
    ])
    ctx = make_context(db=db)

    result = await SecretVaultTool().execute({"operation": "list"}, ctx)

    assert result["count"] == 1
    assert result["entries"] == [{
        "service_key": "myapi:token",
        "display_name": "My API",
        "key_hint": "a****xyz",
        "base_url": "https://api.example.com",
    }]
    assert "encrypted" not in str(result).lower()


@pytest.mark.asyncio
async def test_list_requires_db_session():
    result = await SecretVaultTool().execute({"operation": "list"}, make_context(db=None))
    assert "error" in result


@pytest.mark.asyncio
async def test_save_prompts_user_and_stores_metadata():
    session = FakeSession(result="secret_saved")
    db = FakeDb()
    ctx = make_context(session=session, db=db)

    result = await SecretVaultTool().execute(
        {"operation": "save", "service_key": "jira:email", "display_name": "JIRA"},
        ctx,
    )

    assert result == {"status": "saved", "service_key": "jira:email"}
    # Secret prompt carries the user-token flag; plaintext never goes to the session
    prompt = session.calls[0]
    assert prompt["service_key"] == "jira:email"
    assert prompt["secret"] is True
    assert prompt["save_to_user_tokens"] is True
    # display_name updated via UPDATE + commit
    assert any("UPDATE user_tokens" in s for s, _ in db.executed)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_save_requires_service_key():
    session = FakeSession()
    result = await SecretVaultTool().execute(
        {"operation": "save"}, make_context(session=session)
    )
    assert "service_key is required" in result["error"]
    assert session.calls == []


@pytest.mark.asyncio
async def test_save_prompt_failure_returns_error():
    session = FakeSession(result="secret_save_failed")
    result = await SecretVaultTool().execute(
        {"operation": "save", "service_key": "svc:token"},
        make_context(session=session),
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_delete_confirmed_deletes_row():
    session = FakeSession(result="confirm")
    db = FakeDb()
    ctx = make_context(session=session, db=db)

    result = await SecretVaultTool().execute(
        {"operation": "delete", "service_key": "svc:token"}, ctx
    )

    assert result == {"status": "deleted", "service_key": "svc:token"}
    assert any("DELETE FROM user_tokens" in s for s, _ in db.executed)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_delete_cancelled_by_user():
    session = FakeSession(result="cancel")
    db = FakeDb()

    result = await SecretVaultTool().execute(
        {"operation": "delete", "service_key": "svc:token"},
        make_context(session=session, db=db),
    )

    assert result == {"status": "cancelled", "service_key": "svc:token"}
    assert db.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:8000/admin",   # loopback literal — always blocked
    "http://169.254.169.254/meta",   # cloud metadata — always blocked
    "http://2130706433/",            # inet_aton form of 127.0.0.1
    "file:///etc/passwd",            # non-http scheme
    "not-a-url",                     # no scheme/hostname
    12345,                           # non-string param from the LLM
])
async def test_save_never_writes_unvalidated_base_url(bad_url):
    """base_url is agent-supplied but lands in user_tokens.base_url, which the
    turn kernel feeds to git/Jira/LLM callers as the destination of the user's
    DECRYPTED token — an attacker-chosen URL there exfiltrates the key the
    user just typed. Only URLs passing the REST-path gate may be written."""
    session = FakeSession(result="secret_saved")
    db = FakeDb()
    ctx = make_context(session=session, db=db)

    result = await SecretVaultTool().execute(
        {"operation": "save", "service_key": "git", "base_url": bad_url},
        ctx,
    )

    assert result["status"] == "saved"  # the secret the user typed is kept
    assert result.get("base_url_rejected") is True
    assert not any("base_url = :base_url" in s for s, _ in db.executed)
    assert db.commits == 0


@pytest.mark.asyncio
async def test_save_rejects_base_url_when_dns_check_fails(monkeypatch):
    """The SSRF_ENABLED DNS stage must also gate the write — a hostname that
    resolves private is dropped exactly like a literal private IP."""
    from agent_core.tools import _http, _ssrf
    from agent_core.tools._ssrf import SSRFError

    monkeypatch.setattr(_http, "_is_ssrf_enabled", lambda: True)
    monkeypatch.setattr(
        _ssrf, "resolve_and_validate",
        lambda host, port: (_ for _ in ()).throw(SSRFError("Blocked resolved IP")),
    )

    session = FakeSession(result="secret_saved")
    db = FakeDb()
    result = await SecretVaultTool().execute(
        {"operation": "save", "service_key": "git", "base_url": "https://gw.example.com"},
        make_context(session=session, db=db),
    )

    assert result["status"] == "saved"
    assert result.get("base_url_rejected") is True
    assert not any("base_url = :base_url" in s for s, _ in db.executed)


@pytest.mark.asyncio
async def test_save_writes_normalized_safe_base_url():
    session = FakeSession(result="secret_saved")
    db = FakeDb()
    ctx = make_context(session=session, db=db)

    result = await SecretVaultTool().execute(
        {"operation": "save", "service_key": "git", "base_url": "https://gw.example.com/"},
        ctx,
    )

    assert result == {"status": "saved", "service_key": "git"}
    updates = [(s, p) for s, p in db.executed if "UPDATE user_tokens" in s]
    assert updates and updates[0][1]["base_url"] == "https://gw.example.com"
    assert db.commits == 1
