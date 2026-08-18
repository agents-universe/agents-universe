"""Shared "test this credential against its service" logic.

Used by both ``/api/tokens/{service_key}/test`` (user_tokens) and
``/api/projects/{project_id}/secrets/{secret_id}/test`` (project_secrets)
so the two storage scopes behave identically for git / jira / confluence.
"""
from __future__ import annotations

import traceback

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_service_token(
    service_key: str,
    plain: str,
    base_url: str | None,
    db: AsyncSession,
    user_id: str,
    project_id: str | None = None,
) -> dict:
    """Run a live connectivity check for ``plain`` against the service.

    ``base_url`` is the user's custom endpoint override (``None`` means
    fall back to system config inside each client). ``project_id`` enables
    the ``jira:email`` lookup to fall back to project_secrets when the
    credential lives in a project (agent-configured integrations).
    Never raises: returns ``{"ok": False, "error": ...}`` on failure.
    """
    # service_key may or may not contain ":" (e.g. "git", "jira", "jira:email", "kong:dev")
    provider = service_key.split(":", 1)[0]

    try:
        if provider == "git":
            from api.services.git_client import GitClient
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            git = GitClient(token=plain, base_url=base_url or "")
            async with httpx.AsyncClient(timeout=10.0, trust_env=True, verify=cfg.git_ssl_verify) as http:
                resp = await http.get(f"{git.api_url}/user", headers=git._headers)
            if resp.status_code == 200:
                login = resp.json().get("login", "")
                return {"ok": True, "provider": provider, "login": login}
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}"}

        elif provider == "jira" and ":" not in service_key:
            from api.services.jira_client import JiraClient
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            effective_base = base_url or cfg.atlassian_base_url
            if not effective_base:
                return {"ok": False, "provider": provider, "error": "No base URL configured - set one in integration settings or system config"}
            email = await _resolve_atlassian_email(db, user_id, project_id, cfg)
            if email is None:
                return {"ok": False, "provider": provider, "error": "Jira Email not configured - save it first"}
            jira = JiraClient(api_token=plain, email=email, base_url=base_url or "")
            async with httpx.AsyncClient(timeout=10.0, trust_env=True, verify=cfg.atlassian_ssl_verify) as http:
                resp = await http.get(f"{jira.base_url}/rest/api/2/myself", headers=jira._headers)
            if resp.status_code == 200:
                return {"ok": True, "provider": provider, "display_name": resp.json().get("displayName", "")}
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        elif provider == "confluence" and ":" not in service_key:
            from api.services.confluence_client import ConfluenceClient
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            effective_base = base_url or cfg.atlassian_base_url
            if not effective_base:
                return {"ok": False, "provider": provider, "error": "No base URL configured - set one in integration settings or system config"}
            email = await _resolve_atlassian_email(db, user_id, project_id, cfg)
            if email is None:
                return {"ok": False, "provider": provider, "error": "Jira Email not configured - save it first"}
            conf = ConfluenceClient(api_token=plain, email=email, base_url=base_url or "")
            async with httpx.AsyncClient(timeout=10.0, trust_env=True, verify=cfg.atlassian_ssl_verify) as http:
                resp = await http.get(f"{conf._rest_base}/user/current", headers=conf._headers)
            if resp.status_code == 200:
                return {"ok": True, "provider": provider, "display_name": resp.json().get("displayName", "")}
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

        else:
            # jira:email, kong:*, and other plain values - no live test
            return {"ok": True, "provider": provider, "note": "saved"}

    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "provider": provider, "error": str(exc) or "Token test failed"}


async def _resolve_atlassian_email(db, user_id: str, project_id: str | None, cfg) -> str | None:
    """Resolve the Atlassian account email: user_tokens first, then
    project_secrets (agent-configured integrations may store it per-project).
    Returns ``None`` when the credential needs an email but none is stored."""
    from api.models.user import UserToken
    from api.services.token_vault import decrypt

    if cfg.atlassian_auth_type == "bearer":
        return ""
    email_result = await db.execute(
        select(UserToken).where(
            UserToken.service_key == "jira:email",
            UserToken.user_id == user_id,
        )
    )
    email_token = email_result.scalar_one_or_none()
    if email_token:
        return decrypt(email_token.encrypted_value, user_id)

    if project_id:
        from api.models.project_secret import ProjectSecret
        from api.services.token_vault import decrypt_project_secret

        # Match both the legacy NULL environment and the normalized "" —
        # .first() keeps a pre-normalization duplicate row from raising.
        from sqlalchemy import or_

        ps_result = await db.execute(
            select(ProjectSecret).where(
                ProjectSecret.project_id == project_id,
                ProjectSecret.service_key == "jira:email",
                or_(ProjectSecret.environment.is_(None), ProjectSecret.environment == ""),
                ProjectSecret.secret_name == "default",
                ProjectSecret.is_active == True,  # noqa: E712
            )
        )
        ps = ps_result.scalars().first()
        if ps:
            return decrypt_project_secret(ps.encrypted_value, project_id)
    return None
