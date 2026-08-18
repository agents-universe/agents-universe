"""Regression tests: legacy environment=NULL project secrets.

Save paths normalize a missing environment to "" (SQL Server unique indexes
treat NULL as distinct), but pre-normalization rows hold environment=NULL.
Save and read paths must treat NULL and "" as the same key — no duplicate
rows, no stale reads.
"""
from __future__ import annotations

from sqlalchemy import select

from api.config import get_settings
from api.models.conversation import Conversation
from api.models.project_secret import ProjectSecret
from api.services.token_vault import decrypt_project_secret_or_none, encrypt_project_secret

from agent_core.tools._auth import get_secret
from agent_core.tools.base import ToolContext


async def _add_secret(db, project_id: str, service_key: str, value: str, environment):
    db.add(
        ProjectSecret(
            project_id=project_id,
            service_key=service_key,
            environment=environment,
            secret_name="default",
            encrypted_value=encrypt_project_secret(value, project_id),
            key_hint="h",
            created_by="test-user",
        )
    )
    await db.commit()


async def _rows(db, project_id: str, service_key: str):
    result = await db.execute(
        select(ProjectSecret).where(
            ProjectSecret.project_id == project_id,
            ProjectSecret.service_key == service_key,
        )
    )
    return result.scalars().all()


async def test_create_collapses_legacy_null_row(client, db, make_project):
    """POST without environment updates a legacy NULL row in place — no duplicate."""
    project = await make_project()
    pid = str(project.project_id)
    await _add_secret(db, pid, "kong:dev", "old-token", None)

    resp = await client.post(
        f"/api/projects/{pid}/secrets",
        json={"service_key": "kong:dev", "value": "new-token"},
    )
    assert resp.status_code == 201

    rows = await _rows(db, pid, "kong:dev")
    assert len(rows) == 1
    assert rows[0].environment == ""  # legacy NULL migrated in place
    assert decrypt_project_secret_or_none(rows[0].encrypted_value, pid) == "new-token"


async def test_create_collapses_null_and_empty_duplicates(client, db, make_project):
    """Both NULL and "" rows (pre-fix duplicates) collapse onto the "" row."""
    project = await make_project()
    pid = str(project.project_id)
    await _add_secret(db, pid, "jira", "stale-null", None)
    await _add_secret(db, pid, "jira", "stale-empty", "")

    resp = await client.post(
        f"/api/projects/{pid}/secrets",
        json={"service_key": "jira", "value": "fresh"},
    )
    assert resp.status_code == 201

    rows = await _rows(db, pid, "jira")
    assert len(rows) == 1
    assert rows[0].environment == ""
    assert decrypt_project_secret_or_none(rows[0].encrypted_value, pid) == "fresh"


async def test_save_from_response_collapses_legacy_null(db, make_project):
    """_save_secret_from_response (agent interactive save) matches NULL too."""
    from api.websocket.handlers import _save_secret_from_response

    project = await make_project()
    pid = str(project.project_id)
    cid = "conv-legacy-null"
    db.add(
        Conversation(
            conversation_id=cid, project_id=pid, user_id="test-user"
        )
    )
    await _add_secret(db, pid, "kong:uat", "old-uat", None)

    ok = await _save_secret_from_response(
        cid, "test-user", {"service_key": "kong:uat", "value": "new-uat"}
    )
    assert ok is True

    rows = await _rows(db, pid, "kong:uat")
    assert len(rows) == 1
    assert rows[0].environment == ""
    assert decrypt_project_secret_or_none(rows[0].encrypted_value, pid) == "new-uat"


async def _tool_ctx(db, project_id: str) -> ToolContext:
    return ToolContext(
        project_id=project_id,
        project_fs_path="",
        conversation_id="c",
        user_id="test-user",
        db_session=db,
        secret_key=get_settings().secret_key,
    )


async def test_get_secret_reads_legacy_null_row(db, make_project):
    """Env-agnostic read must resolve a lone legacy NULL row (Bug 4 regression)."""
    project = await make_project()
    pid = str(project.project_id)
    await _add_secret(db, pid, "kong:dev", "legacy-token", None)

    val = await get_secret(await _tool_ctx(db, pid), "kong:dev")
    assert val == "legacy-token"


async def test_get_secret_prefers_normalized_row_over_legacy_null(db, make_project):
    """With duplicate rows, the "" row (newer semantic) wins over the NULL row."""
    project = await make_project()
    pid = str(project.project_id)
    await _add_secret(db, pid, "kong:dev", "stale-null", None)
    await _add_secret(db, pid, "kong:dev", "fresh-empty", "")

    val = await get_secret(await _tool_ctx(db, pid), "kong:dev")
    assert val == "fresh-empty"
