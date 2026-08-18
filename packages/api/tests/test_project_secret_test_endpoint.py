"""Tests for POST /api/projects/{project_id}/secrets/{secret_id}/test —
the project-scoped equivalent of /api/tokens/{key}/test (agent-configured
integrations get the same live connectivity checks)."""
from __future__ import annotations


async def _create_secret(client, project, service_key: str, value: str = "topsecret"):
    resp = await client.post(
        f"/api/projects/{project.project_id}/secrets",
        json={"service_key": service_key, "value": value},
    )
    assert resp.status_code == 201
    return resp.json()


async def test_secret_test_plain_keys_return_saved(client, make_project):
    """kong:dev-style keys have no live check — always report saved."""
    project = await make_project()
    secret = await _create_secret(client, project, "kong:dev")

    resp = await client.post(
        f"/api/projects/{project.project_id}/secrets/{secret['secret_id']}/test"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["note"] == "saved"


async def test_secret_test_jira_errors_without_network(client, make_project):
    """Jira fails fast without a network call when config is incomplete."""
    project = await make_project()
    secret = await _create_secret(client, project, "jira")

    resp = await client.post(
        f"/api/projects/{project.project_id}/secrets/{secret['secret_id']}/test"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    # Either fail-fast path is fine (depends on env config), as long as no
    # HTTP call is attempted: missing base URL, or missing jira:email.
    assert ("No base URL configured" in body["error"]
            or "Jira Email not configured" in body["error"])


async def test_secret_test_unknown_secret_404(client, make_project):
    project = await make_project()
    resp = await client.post(
        f"/api/projects/{project.project_id}/secrets/does-not-exist/test"
    )
    assert resp.status_code == 404
