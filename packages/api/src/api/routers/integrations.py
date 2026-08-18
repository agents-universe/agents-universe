"""Integration endpoints - proxy access to Jira, Confluence, Git, Kong.

These endpoints allow the agent tools and frontend to call external services.
Tokens are read from the user's encrypted records in user_tokens table.
Base URLs come from user config (UserToken.base_url) or fall back to system
config (env vars).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.user import UserToken
from api.services.token_vault import decrypt

# SSRF validation for user-controlled outbound URLs (Kong proxy). Literal-IP
# checks run unconditionally; DNS resolution follows the SSRF_ENABLED opt-in
# convention shared with agent-core's outbound http client.
from agent_core.tools._ssrf import SSRFError

router = APIRouter(prefix="/api/integrations")

_log = logging.getLogger("agents_universe.integrations")


def _external_error(service: str, e: Exception) -> HTTPException:
    """Log the real exception server-side; return a generic 502 to the client.

    Raw exception strings from external services may contain internal URLs,
    hostnames, or auth hints - never send them to the client.
    """
    _log.error("External service call failed (%s)", service, exc_info=e)
    return HTTPException(status_code=502, detail=f"External service unavailable: {service}")


# ── System defaults ───────────────────────────────────────────────────────

@router.get("/defaults")
async def get_integration_defaults(
    current_user: UserInfo = Depends(get_current_user),
):
    """Return system-configured base URLs so the frontend can show them as
    default / placeholder values when users add integrations."""
    settings = get_settings()
    defaults: dict[str, str] = {}
    if settings.git_base_url:
        defaults["git"] = settings.git_base_url
    if settings.atlassian_base_url:
        defaults["jira"] = settings.atlassian_base_url
        defaults["confluence"] = settings.atlassian_base_url
    return defaults


# ── Token + base_url helpers ──────────────────────────────────────────────

async def _get_user_token(db: AsyncSession, user_id: str, service_key: str) -> str:
    """Decrypt and return a user's token for a given service key."""
    token, _ = await _get_user_token_and_base_url(db, user_id, service_key)
    return token


async def _resolve_atlassian_email(db: AsyncSession, user_id: str) -> str:
    """Email is only needed for Jira basic auth; bearer/pat setups never
    send it, so requiring jira:email there would 400 every integration call."""
    from api.config import get_settings
    if get_settings().atlassian_auth_type == "basic":
        return await _get_user_token(db, user_id, "jira:email")
    return ""


async def _get_user_token_and_base_url(
    db: AsyncSession, user_id: str, service_key: str,
) -> tuple[str, str | None]:
    """Decrypt and return a user's token **and** custom base_url for a service."""
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_id,
            UserToken.service_key == service_key,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail=f"Token not configured: {service_key}")
    base_url = row.base_url
    if base_url:
        # SSRF guard on user-supplied base URLs (same gate as the Kong proxy
        # below): a misconfigured or malicious base_url must not turn these
        # endpoints into an open proxy against internal hosts. Empty falls
        # back to system config, which is trusted.
        try:
            from agent_core.tools._ssrf import validate_url as ssrf_validate_url
            ssrf_validate_url(base_url, allow_any_port=True)
            # DNS-level check per SSRF_ENABLED — resolve_and_validate directly
            # (validate_outbound_url would re-apply the port allowlist and
            # defeat allow_any_port; same policy as tokens.py/model_configs.py/
            # episodic_service.py.
            from agent_core.tools._http import _is_ssrf_enabled
            if _is_ssrf_enabled():
                from agent_core.tools._ssrf import resolve_and_validate
                from urllib.parse import urlparse
                _p = urlparse(base_url)
                resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))
        except SSRFError as e:
            _log.warning("integration %s: blocked outbound base_url: %s", service_key, e)
            raise HTTPException(status_code=400, detail=f"Blocked URL: {e}")
    try:
        plain = decrypt(row.encrypted_value, user_id)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=f"Stored token is corrupted — delete and re-save it: {service_key}",
        )
    return plain, base_url


# ── Jira ─────────────────────────────────────────────────────────────────

class JiraCommentBody(BaseModel):
    body: str


def _jira_client(email: str, token: str, base_url: str | None = None):
    from api.services.jira_client import JiraClient
    return JiraClient(email=email, api_token=token, base_url=base_url or "")


@router.get("/jira/issue/{issue_key}")
async def get_jira_issue(
    issue_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "jira")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = _jira_client(email, token, base_url)
        return await client.get_issue(issue_key)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("jira", e)


@router.get("/jira/issue/{issue_key}/comments")
async def get_jira_comments(
    issue_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "jira")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = _jira_client(email, token, base_url)
        return await client.get_issue_comments(issue_key)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("jira", e)


@router.get("/jira/issue/{issue_key}/transitions")
async def get_jira_transitions(
    issue_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "jira")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = _jira_client(email, token, base_url)
        return await client.get_issue_transitions(issue_key)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("jira", e)


@router.post("/jira/issue/{issue_key}/comment")
async def add_jira_comment(
    issue_key: str,
    body: JiraCommentBody,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "jira")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = _jira_client(email, token, base_url)
        return await client.add_comment(issue_key, body.body)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("jira", e)


@router.get("/jira/search")
async def search_jira(
    jql: str = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "jira")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = _jira_client(email, token, base_url)
        return await client.search_issues(jql)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("jira", e)


# ── Confluence ───────────────────────────────────────────────────────────

@router.get("/confluence/page/{page_id}")
async def get_confluence_page(
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.confluence_client import ConfluenceClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "confluence")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = ConfluenceClient(email=email, api_token=token, base_url=base_url or "")
        page = await client.get_page(page_id)
        body_html = page.get("body", {}).get("storage", {}).get("value", "")
        return {
            "id": page.get("id"),
            "title": page.get("title"),
            "body_html": body_html,
            "body_text": ConfluenceClient.html_to_text(body_html),
            "version": page.get("version", {}).get("number"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("confluence", e)


@router.get("/confluence/tree/{root_page_id}")
async def get_confluence_tree(
    root_page_id: str,
    max_pages: int = Query(100, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.confluence_client import ConfluenceClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "confluence")
    email = await _resolve_atlassian_email(db, current_user.user_id)
    try:
        client = ConfluenceClient(email=email, api_token=token, base_url=base_url or "")
        return await client.get_page_tree(root_page_id, max_pages=max_pages)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("confluence", e)


# ── Git (GitHub Enterprise) ──────────────────────────────────────────────

@router.get("/git/user")
async def get_git_user(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.git_client import GitClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "git")
    try:
        client = GitClient(token=token, base_url=base_url or "")
        return await client.get_user()
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("git", e)


@router.get("/git/repos/{owner}/{repo}/pulls")
async def list_git_prs(
    owner: str,
    repo: str,
    state: str = Query("open"),
    author: str | None = Query(None),
    reviewer: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.git_client import GitClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "git")
    try:
        client = GitClient(token=token, base_url=base_url or "")
        return await client.list_prs(owner, repo, state=state, author=author, reviewer=reviewer)
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("git", e)


@router.get("/git/repos/{owner}/{repo}/pulls/{number}")
async def get_git_pr(
    owner: str,
    repo: str,
    number: int,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.git_client import GitClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "git")
    try:
        client = GitClient(token=token, base_url=base_url or "")
        pr = await client.get_pr(owner, repo, number)
        files = await client.get_pr_files(owner, repo, number)
        commits = await client.get_pr_commits(owner, repo, number)
        return {"pr": pr, "files": files, "commits": commits}
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("git", e)


@router.get("/git/repos/{owner}/{repo}/commits/{sha}/checks")
async def get_commit_checks(
    owner: str,
    repo: str,
    sha: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    from api.services.git_client import GitClient
    token, base_url = await _get_user_token_and_base_url(db, current_user.user_id, "git")
    try:
        client = GitClient(token=token, base_url=base_url or "")
        status = await client.get_commit_status(owner, repo, sha)
        check_runs = await client.get_check_runs(owner, repo, sha)
        return {"combined_status": status, "check_runs": check_runs}
    except HTTPException:
        raise
    except Exception as e:
        raise _external_error("git", e)


# ── Kong ─────────────────────────────────────────────────────────────────

class KongRequestBody(BaseModel):
    method: str = "GET"
    path: str
    base_url: str | None = None
    body: dict | None = None
    env: str = "dev"


@router.post("/kong/request")
async def kong_request(
    req: KongRequestBody,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    import httpx

    service_key = f"kong:{req.env}" if req.env in ("dev", "uat", "int") else "kong:dev"
    token = await _get_user_token(db, current_user.user_id, service_key)

    settings = get_settings()
    base = req.base_url or settings.app_base_url
    url = f"{base.rstrip('/')}{req.path}"

    # SSRF guard: scheme/port/literal-IP checks are always on (private IPs,
    # metadata hosts and non-standard ports are rejected even when the target
    # is a legitimate gateway); DNS-resolved checks follow the SSRF_ENABLED
    # opt-in convention used by the rest of the framework, so internal Kong
    # hostnames that resolve to private IPs keep working by default.
    try:
        from agent_core.tools._ssrf import validate_url as ssrf_validate_url
        ssrf_validate_url(url, allow_any_port=True)
        # DNS-level check per SSRF_ENABLED — resolve_and_validate directly
        # (validate_outbound_url would re-apply the port allowlist.
        from agent_core.tools._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from agent_core.tools._ssrf import resolve_and_validate
            from urllib.parse import urlparse
            _p = urlparse(url)
            resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))
    except SSRFError as e:
        _log.warning("kong_request: blocked outbound URL: %s", e)
        raise HTTPException(status_code=400, detail=f"Blocked URL: {e}")

    headers = {"x-api-key": token, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if req.method.upper() == "GET":
                resp = await client.get(url, headers=headers)
            elif req.method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=req.body)
            elif req.method.upper() == "PUT":
                resp = await client.put(url, headers=headers, json=req.body)
            elif req.method.upper() == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported method: {req.method}")

        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                body = resp.text
        else:
            body = resp.text
        return {"status": resp.status_code, "body": body}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        return {"status": e.response.status_code, "body": e.response.text}
    except Exception as e:
        raise _external_error("kong", e)
