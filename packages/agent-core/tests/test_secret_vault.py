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
