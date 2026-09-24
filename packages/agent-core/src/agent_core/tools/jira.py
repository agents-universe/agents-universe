"""Jira tool — full Jira REST API access for the agent."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from .base import Tool, ToolContext
from ._auth import ToolAuthError, get_token, get_token_optional
from ._http import _header_value_problem, ensure_http_client
from .shell import redact_secrets
from ._wiki import markdown_to_wiki

_log = logging.getLogger(__name__)


def _redact(client: "_JiraClient", text: str) -> str:
    """Scrub the resolved credential out of text bound for the LLM or the log.

    Atlassian echoes the credential in error bodies and SSO redirects, so every
    message that can carry an upstream response must pass through here before
    it is returned or logged (same pattern as kong.py / api_request.py).
    """
    return redact_secrets(text, {
        # getattr: the aggregate op is exercised with duck-typed clients in tests.
        "jira": getattr(client, "api_token", ""),
        "jira:email": getattr(client, "email", ""),
    })


def _http_error_message(client: "_JiraClient", e: httpx.HTTPStatusError,
                        operation: str = "") -> str:
    """Status + redacted body for an HTTP error.

    Shared by execute()'s handler and get_issue_context's per-section error
    path: the aggregate collects section exceptions instead of letting them
    propagate, so it never reaches that handler and must redact for itself.
    Kept in one place so the two cannot drift.
    """
    status = e.response.status_code if e.response is not None else "error"
    body = _redact(client, e.response.text[:500])[:500] if e.response is not None else ""
    _log.warning("jira %s HTTP %s: %s", operation, status, body[:200])
    return f"Jira API returned {status}: {body}"


def _failure_text(client: "_JiraClient", e: BaseException, operation: str) -> str:
    """Message for an exception that is reported in place instead of raised.

    HTTP errors keep their status (and get logged) through the shared
    formatter; anything else falls back to the redacted exception text.
    """
    if isinstance(e, httpx.HTTPStatusError):
        return _http_error_message(client, e, operation)
    return _redact(client, str(e))[:500]


# attach_file reads the whole file into memory, and Atlassian's own attachment
# limit is instance-configured and often lower than this — past it the upload
# is a slow way to get a 413.
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _attachment_too_large(rel_path: str, size: int, *, upstream: bool = False) -> dict[str, Any]:
    reason = (
        "Jira rejected the upload (413)"
        if upstream
        else f"{size / (1024 * 1024):.1f}MB exceeds the "
        f"{_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB attachment limit"
    )
    return {
        "error": (
            f"Attachment too large: {reason} ({rel_path}). Attach the Playwright "
            "trace (tests/test-results/**/trace.zip) or a trimmed clip/screenshot "
            "instead, and note the substitution in the issue comment."
        ),
        "file_path": rel_path,
        "size": size,
    }


def _jql_literal(value: Any) -> str:
    """Quote a value as a JQL string literal.

    Unquoted interpolation let a version id / project key containing spaces or
    quotes alter the JQL structure; the double-quote escape is JQL's own.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# The three card reads below share their projections with get_issue_context so
# the caps (description 20000, comment body 2000) cannot drift apart between
# the single and the aggregate call.

def _project_issue(data: dict, base_url: str) -> dict:
    fields = data.get("fields", {})
    return {
        "key": data.get("key"),
        "summary": fields.get("summary"),
        "status": fields.get("status", {}).get("name"),
        "issue_type": fields.get("issuetype", {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "labels": fields.get("labels", []),
        # Cap the description — a giant body must not flood the LLM context.
        "description": (fields.get("description", "") or "")[:20000],
        "acceptance_criteria": fields.get("customfield_10028", ""),
        "url": f"{base_url}/browse/{data.get('key')}",
    }


# A single-comment read costs one comment of context, so it can afford a bigger
# cap than a comment list (where every body in the list loads at once).
_SINGLE_COMMENT_CAP = 8000


def _project_comment(c: dict, cap: int = 2000) -> dict:
    body = c.get("body", "") or ""
    out: dict[str, Any] = {
        "id": c.get("id"),
        "author": c.get("author", {}).get("displayName", ""),
        "body": body[:cap],
        "created": c.get("created"),
    }
    if len(body) > cap:
        # Say so: a silently truncated read-back looks like the write itself was
        # cut, and the read-back exists precisely to prove that it was not.
        out["truncated"] = True
        out["body_length"] = len(body)
    return out


def _project_comments(key: str, comments: list[dict]) -> dict:
    return {"issue_key": key, "comments": [_project_comment(c) for c in comments]}


def _project_transitions(key: str, transitions: list[dict]) -> dict:
    return {"issue_key": key, "transitions": [
        {"id": t["id"], "name": t["name"], "to": t.get("to", {}).get("name")}
        for t in transitions
    ]}


class JiraTool(Tool):
    name = "jira"
    prompt_hint = (
        "First stop for any task referencing a Jira key — issue, comments, "
        "transitions, and linked PRs. The only path to Jira: read/create/update "
        "issues, comments, transitions, and test cycles. Ask the user for issue "
        "keys or project names instead of guessing. When you need a card's full "
        "context, prefer get_issue_context over three separate reads; to check a "
        "comment you just wrote, get_comment by its id, not the whole comment list."
    )
    description = (
        "Interact with Jira: fetch a card's full context (issue+comments+transitions) "
        "in one call or read each separately, create/update issues, "
        "manage test cycles, upload attachments, link issues, transition status, and search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "get_issue_context", "get_issue", "get_comments", "get_comment",
                    "get_transitions", "get_release_scope",
                    "create_issue", "create_test_issue", "create_test_cycle",
                    "update_description", "update_assignee",
                    "add_comment", "add_attachment", "link_issues",
                    "transition_issue", "search",
                ],
            },
            "issue_key": {"type": "string", "description": "Jira issue key, e.g. DDM-1234"},
            "comment_id": {
                "type": "string",
                "description": (
                    "Comment id for get_comment — the id add_comment returned, to "
                    "read back just that comment instead of the whole list"
                ),
            },
            "summary": {"type": "string"},
            "description": {"type": "string"},
            "project_key": {"type": "string"},
            "issue_type": {"type": "string", "default": "Task"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "transition_name": {"type": "string"},
            "file_path": {
                "type": "string",
                "description": "Relative path for attachments (workspace-relative, 50MB max)",
            },
            "jql": {"type": "string", "description": "JQL query for search"},
            "link_type": {"type": "string", "default": "Tests"},
            "target_issue_key": {"type": "string", "description": "Target issue for linking/test creation"},
            "from_key": {"type": "string"},
            "to_key": {"type": "string"},
            "version_id": {"type": "string"},
            "release_url": {"type": "string"},
            "assignee_name": {"type": "string"},
            "assignee_account_id": {"type": "string"},
            "comment_body": {"type": "string", "description": "Comment body in Markdown — auto-converted to Jira wiki markup on write"},
            "cycle_name": {"type": "string"},
            "cycle_project_id": {"type": "string"},
            "test_kind": {"type": "string", "enum": ["api", "ui"]},
            "max_results": {"type": "integer", "default": 50},
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]

        try:
            client = await self._build_client(context)
        except ToolAuthError as e:
            return {"error": str(e)}

        try:
            handler = getattr(self, f"_op_{operation}", None)
            if not handler:
                return {"error": f"Unknown operation: {operation}"}
            return await handler(params, client, context)
        except httpx.HTTPStatusError as e:
            return {"error": _http_error_message(client, e, operation)}
        except Exception as e:
            message = _failure_text(client, e, operation)
            _log.warning("jira %s failed: %s", operation, message, exc_info=True)
            return {"error": f"Jira operation failed: {message}"}

    async def _build_client(self, context: ToolContext) -> "_JiraClient":
        token = await get_token(context, "jira")
        email = await get_token_optional(context, "jira:email") or ""
        base_url = context.cfg("ATLASSIAN_BASE_URL")
        if not base_url:
            raise ToolAuthError("jira", "ATLASSIAN_BASE_URL is not configured — set it in Settings → Integrations → Jira")
        jira_path = context.cfg("JIRA_BASE_PATH")
        auth_type = context.cfg("ATLASSIAN_AUTH_TYPE", "basic")
        if auth_type == "bearer":
            # Basic mode base64-absorbs control characters; bearer puts the
            # token on the wire verbatim, and h11 echoes an illegal value
            # back ("Illegal header value b'...'") — which IS the credential,
            # into execute()'s exc_info warning. Validate before the transport
            # ever sees it; the message names only the header.
            problem = _header_value_problem(f"Bearer {token}")
            if problem:
                raise ToolAuthError(
                    "jira",
                    message=(
                        f"Jira Authorization header {problem} — "
                        "re-save the stored token without it"
                    ),
                )
        http = ensure_http_client(context, target_url=base_url)
        return _JiraClient(
            api_token=token, email=email,
            base_url=base_url, jira_path=jira_path,
            auth_type=auth_type, http=http,
        )

    # --- Operations ---

    async def _op_get_issue(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        if not key:
            return {"error": "issue_key is required"}
        data = await client.get_issue(key)
        return _project_issue(data, client.base_url)

    async def _op_get_comments(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        if not key:
            return {"error": "issue_key is required"}
        return _project_comments(key, await client.get_comments(key))

    async def _op_get_transitions(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        if not key:
            return {"error": "issue_key is required"}
        return _project_transitions(key, await client.get_transitions(key))

    async def _op_get_comment(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        """One comment by id — the cheapest way to read back a comment just written.

        ``add_comment`` already returns the id, so verifying a writeback costs one
        small fetch instead of pulling every comment on a card that may carry
        dozens (each re-sent to the model on every following turn).
        """
        key = params.get("issue_key", "")
        comment_id = str(params.get("comment_id", "") or "")
        if not key or not comment_id:
            return {"error": "issue_key and comment_id are required"}
        data = await client.get_comment(key, comment_id)
        return {"issue_key": key, "comment": _project_comment(data, _SINGLE_COMMENT_CAP)}

    async def _op_get_issue_context(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        """Issue + comments + transitions in one call.

        The three reads are independent, so one round trip replaces three —
        the card-first rule in the QA workflow otherwise costs three serial
        agent turns before any design work starts. A failing section is
        reported in place (``comments_error``) instead of sinking the whole
        payload: the description alone is still worth designing from.
        """
        key = params.get("issue_key", "")
        if not key:
            return {"error": "issue_key is required"}
        issue, comments, transitions = await asyncio.gather(
            client.get_issue(key),
            client.get_comments(key),
            client.get_transitions(key),
            return_exceptions=True,
        )

        result: dict[str, Any] = {"issue_key": key}
        if isinstance(issue, BaseException):
            # A section error skips execute()'s handler entirely, so it has to
            # be redacted here rather than there. Without the issue there is no
            # card to design from, so this one is fatal.
            return {"error": f"Jira operation failed: {_failure_text(client, issue, 'get_issue_context')}",
                    "issue_key": key}
        result.update(_project_issue(issue, client.base_url))
        if isinstance(comments, BaseException):
            result["comments_error"] = _failure_text(client, comments, "get_issue_context")
        else:
            result.update(_project_comments(key, comments))
        if isinstance(transitions, BaseException):
            result["transitions_error"] = _failure_text(client, transitions, "get_issue_context")
        else:
            result.update(_project_transitions(key, transitions))
        return result

    async def _op_get_release_scope(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        version_id = params.get("version_id")
        project_key = params.get("project_key")
        if not version_id and not params.get("release_url"):
            return {"error": "version_id or release_url is required"}
        # Scope by project when provided - a bare fixVersion match crosses
        # every project the token can see. Literals are quoted (see
        # _jql_literal) so values cannot alter the JQL structure.
        clauses = [f"fixVersion = {_jql_literal(version_id)}"]
        if project_key:
            clauses.append(f"project = {_jql_literal(project_key)}")
        jql = " AND ".join(clauses)
        if not version_id:
            return {"error": "release_url parsing not yet supported — use version_id"}
        max_results = params.get("max_results", 50)
        issues = await client.search(jql, max_results=max_results)
        return {
            "version_id": version_id,
            "total": len(issues),
            "issues": [
                {"key": i["key"], "summary": i["fields"]["summary"],
                 "status": i["fields"].get("status", {}).get("name"),
                 "type": i["fields"].get("issuetype", {}).get("name")}
                for i in issues
            ],
        }

    async def _op_create_issue(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        # JIRA_PROJECT_KEY is a project name, not a credential — cfg()'s
        # credential-key guard (final segment "KEY") would never return it.
        project_key = params.get("project_key") or ctx.integration_settings.get("JIRA_PROJECT_KEY")
        summary = params.get("summary", "")
        if not summary:
            return {"error": "summary is required"}
        if not project_key:
            return {"error": "project_key is required"}
        result = await client.create_issue(
            project_key=project_key,
            summary=summary,
            description=params.get("description", ""),
            issue_type=params.get("issue_type", "Task"),
            labels=params.get("labels"),
        )
        return {"key": result.get("key"), "id": result.get("id"),
                "url": f"{client.base_url}/browse/{result.get('key')}"}

    async def _op_create_test_issue(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        target = params.get("target_issue_key", "")
        summary = params.get("summary", "")
        if not target or not summary:
            return {"error": "target_issue_key and summary are required"}
        project_key = params.get("project_key") or ctx.integration_settings.get("JIRA_PROJECT_KEY")
        if not project_key:
            return {"error": "project_key is required"}
        issue_type = ctx.cfg("JIRA_TEST_ISSUE_TYPE", "Test")
        link_type = params.get("link_type") or ctx.cfg("JIRA_TEST_LINK_TYPE", "Tests")

        result = await client.create_issue(
            project_key=project_key,
            summary=summary,
            description=params.get("description", ""),
            issue_type=issue_type,
            labels=params.get("labels"),
        )
        new_key = result.get("key", "")
        if new_key and target:
            await client.link_issues(new_key, target, link_type)
        return {"key": new_key, "linked_to": target, "link_type": link_type,
                "url": f"{client.base_url}/browse/{new_key}"}

    async def _op_create_test_cycle(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        name = params.get("cycle_name", "")
        project_id = params.get("cycle_project_id", "")
        version_id = params.get("version_id", "")
        if not name or not project_id or not version_id:
            return {"error": "cycle_name, cycle_project_id, and version_id are required"}
        try:
            project_id_int = int(project_id)
            version_id_int = int(version_id)
        except ValueError:
            return {"error": "cycle_project_id and version_id must be numeric Jira IDs"}
        result = await client.create_test_cycle(
            name=name, project_id=project_id_int, version_id=version_id_int,
            description=params.get("description", ""),
        )
        return result

    async def _op_update_description(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        desc = params.get("description", "")
        if not key or not desc:
            return {"error": "issue_key and description are required"}
        await client.update_issue(key, {"description": desc})
        return {"success": True, "issue_key": key}

    async def _op_update_assignee(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        account_id = params.get("assignee_account_id")
        name = params.get("assignee_name")
        if not key:
            return {"error": "issue_key is required"}
        if not account_id and not name:
            return {"error": "assignee_account_id or assignee_name is required"}
        if account_id:
            await client.update_issue(key, {"assignee": {"accountId": account_id}})
        else:
            await client.update_issue(key, {"assignee": {"name": name}})
        return {"success": True, "issue_key": key}

    async def _op_add_comment(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        body = params.get("comment_body", "") or params.get("description", "")
        if not key or not body:
            return {"error": "issue_key and comment_body are required"}
        result = await client.add_comment(key, body)
        return {"success": True, "comment_id": result.get("id"), "issue_key": key}

    async def _op_add_attachment(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        rel_path = params.get("file_path", "")
        if not key or not rel_path:
            return {"error": "issue_key and file_path are required"}
        base = Path(ctx.project_fs_path).resolve()
        full_path = (base / rel_path).resolve()
        if not full_path.is_relative_to(base):
            return {"error": f"Access denied: path {rel_path!r} is outside project scope"}
        if not full_path.exists():
            return {"error": f"File not found: {rel_path}"}
        if not full_path.is_file():
            return {"error": f"Not a file: {rel_path}"}
        size = full_path.stat().st_size
        if size > _MAX_ATTACHMENT_BYTES:
            return _attachment_too_large(rel_path, size)
        try:
            result = await client.attach_file(key, str(full_path))
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 413:
                # The instance's own limit is often lower than ours — the fix
                # for the agent is the same either way.
                _log.warning("jira add_attachment got 413 for %s", rel_path)
                return _attachment_too_large(rel_path, size, upstream=True)
            raise
        return {"success": True, "issue_key": key, "attachments": result}

    async def _op_link_issues(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        from_key = params.get("from_key", "")
        to_key = params.get("to_key", "")
        link_type = params.get("link_type", "Tests")
        if not from_key or not to_key:
            return {"error": "from_key and to_key are required"}
        await client.link_issues(from_key, to_key, link_type)
        return {"success": True, "from": from_key, "to": to_key, "type": link_type}

    async def _op_transition_issue(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        key = params.get("issue_key", "")
        name = params.get("transition_name", "")
        if not key or not name:
            return {"error": "issue_key and transition_name are required"}
        transitions = await client.get_transitions(key)
        target = next((t for t in transitions if t.get("name", "").lower() == name.lower()), None)
        if not target:
            available = [t.get("name", "") for t in transitions if t.get("name")]
            return {"error": f"Transition '{name}' not found. Available: {available}"}
        await client.transition_issue(key, target["id"])
        return {"success": True, "issue_key": key, "transitioned_to": target.get("to", {}).get("name", name)}

    async def _op_search(self, params: dict, client: "_JiraClient", ctx: ToolContext) -> dict:
        jql = params.get("jql", "")
        if not jql:
            return {"error": "jql is required"}
        max_results = params.get("max_results", 50)
        issues = await client.search(jql, max_results=max_results)
        return {"total": len(issues), "issues": [
            {"key": i["key"], "summary": i["fields"]["summary"],
             "status": i["fields"].get("status", {}).get("name"),
             "type": i["fields"].get("issuetype", {}).get("name")}
            for i in issues
        ]}


class _JiraClient:
    """Lightweight Jira client using shared httpx client."""

    _MAX_RESPONSE = 2_000_000  # bytes — reads cap out far below confluence's

    def __init__(self, api_token: str, email: str, base_url: str,
                 jira_path: str, auth_type: str, http: httpx.AsyncClient):
        base = base_url.rstrip("/")
        path = jira_path.rstrip("/") if jira_path else ""
        self.base_url = f"{base}{path}" if base else ""
        self.email = email
        self.api_token = api_token
        self.auth_type = auth_type
        self._http = http

    @property
    def _headers(self) -> dict[str, str]:
        if self.auth_type == "bearer":
            auth = f"Bearer {self.api_token}"
        else:
            cred = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
            auth = f"Basic {cred}"
        return {"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"}

    async def _get_capped(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
    ) -> dict:
        """Read a Jira response with a hard byte cap — a huge issue (giant
        description, long comments) must not be buffered whole and stuffed
        into the LLM context / persisted history."""
        async with self._http.stream(
            method, url, headers=self._headers, json=json_body
        ) as resp:
            resp.raise_for_status()
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > self._MAX_RESPONSE:
                    raise ValueError(
                        f"Jira response too large (>{self._MAX_RESPONSE} bytes)"
                    )
                chunks.append(chunk)
        import json
        return json.loads(b"".join(chunks).decode("utf-8", errors="replace"))

    async def get_issue(self, key: str) -> dict:
        return await self._get_capped(f"{self.base_url}/rest/api/2/issue/{key}")

    async def get_comments(self, key: str) -> list[dict]:
        return (await self._get_capped(
            f"{self.base_url}/rest/api/2/issue/{key}/comment")).get("comments", [])

    async def get_comment(self, key: str, comment_id: str) -> dict:
        return await self._get_capped(
            f"{self.base_url}/rest/api/2/issue/{key}/comment/{comment_id}")

    async def get_transitions(self, key: str) -> list[dict]:
        return (await self._get_capped(
            f"{self.base_url}/rest/api/2/issue/{key}/transitions")).get("transitions", [])

    async def create_issue(self, project_key: str, summary: str, description: str = "",
                           issue_type: str = "Task", labels: list[str] | None = None) -> dict:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            # v2 API renders string bodies as wiki markup, not Markdown.
            fields["description"] = markdown_to_wiki(description)
        if labels:
            fields["labels"] = labels
        resp = await self._http.post(
            f"{self.base_url}/rest/api/2/issue", headers=self._headers, json={"fields": fields})
        resp.raise_for_status()
        return resp.json()

    async def update_issue(self, key: str, fields: dict) -> None:
        if "description" in fields and fields["description"]:
            fields = {**fields, "description": markdown_to_wiki(fields["description"])}
        resp = await self._http.put(
            f"{self.base_url}/rest/api/2/issue/{key}", headers=self._headers, json={"fields": fields})
        resp.raise_for_status()

    async def add_comment(self, key: str, body: str) -> dict:
        resp = await self._http.post(
            f"{self.base_url}/rest/api/2/issue/{key}/comment",
            # v2 API renders string bodies as wiki markup, not Markdown.
            headers=self._headers, json={"body": markdown_to_wiki(body)})
        resp.raise_for_status()
        return resp.json()

    async def link_issues(self, from_key: str, to_key: str, link_type: str = "Tests") -> None:
        resp = await self._http.post(
            f"{self.base_url}/rest/api/2/issueLink", headers=self._headers,
            json={"type": {"name": link_type}, "inwardIssue": {"key": from_key}, "outwardIssue": {"key": to_key}})
        resp.raise_for_status()

    async def transition_issue(self, key: str, transition_id: str) -> None:
        resp = await self._http.post(
            f"{self.base_url}/rest/api/2/issue/{key}/transitions",
            headers=self._headers, json={"transition": {"id": transition_id}})
        resp.raise_for_status()

    async def attach_file(self, key: str, file_path: str) -> list[dict]:
        headers = self._headers.copy()
        headers.pop("Content-Type")
        headers["X-Atlassian-Token"] = "no-check"
        path = Path(file_path)
        # Read into memory on a worker thread: httpx's multipart stream
        # calls file.read() synchronously inside the event loop, so a big
        # attachment would block every concurrent task.
        data = await asyncio.to_thread(path.read_bytes)
        resp = await self._http.post(
            f"{self.base_url}/rest/api/2/issue/{key}/attachments",
            headers=headers,
            files={"file": (path.name, data, "application/octet-stream")},
        )
        resp.raise_for_status()
        return resp.json()

    async def search(self, jql: str, max_results: int = 50) -> list[dict]:
        return (await self._get_capped(
            f"{self.base_url}/rest/api/2/search",
            method="POST",
            json_body={"jql": jql, "maxResults": max_results},
        )).get("issues", [])

    async def create_test_cycle(self, name: str, project_id: int, version_id: int,
                                description: str = "") -> dict:
        payload = {"name": name, "projectId": project_id, "versionId": version_id}
        if description:
            payload["description"] = markdown_to_wiki(description)
        resp = await self._http.post(
            f"{self.base_url}/rest/zapi/latest/cycle", headers=self._headers, json=payload)
        resp.raise_for_status()
        return resp.json()
