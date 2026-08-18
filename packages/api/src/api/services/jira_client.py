"""Jira REST API client — used by skills and tools to interact with Jira.

Supports Jira Cloud and Jira Server (v2 API).
Base URL from system config; credentials from user DB.
Never logs or returns credentials.
"""
from __future__ import annotations

import base64
from typing import Any

import httpx

from api.config import get_settings


class JiraClient:
    """Async Jira API client."""

    def __init__(self, api_token: str, email: str = "", base_url: str = "", auth_type: str = ""):
        settings = get_settings()
        base = (base_url or settings.atlassian_base_url).rstrip("/")
        jira_path = settings.atlassian_jira_base_path.rstrip("/")
        self.base_url = f"{base}{jira_path}"
        self.email = email
        self.api_token = api_token
        self.auth_type = auth_type or settings.atlassian_auth_type
        self.timeout = 30.0
        # internal endpoints (Jira Server, Gitea, Confluence)
        # frequently use self-signed certs; the verify flag was configured in
        # settings but never passed to the per-call clients.
        self.ssl_verify = settings.atlassian_ssl_verify

    @property
    def _headers(self) -> dict[str, str]:
        if self.auth_type == "bearer":
            auth = f"Bearer {self.api_token}"
        else:
            cred = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode()
            auth = f"Basic {cred}"
        return {
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.base_url}/rest/api/2/issue/{issue_key}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_issue_comments(self, issue_key: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.base_url}/rest/api/2/issue/{issue_key}/comment",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("comments", [])

    async def get_issue_transitions(self, issue_key: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.base_url}/rest/api/2/issue/{issue_key}/transitions",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json().get("transitions", [])

    async def add_comment(self, issue_key: str, body: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.base_url}/rest/api/2/issue/{issue_key}/comment",
                headers=self._headers,
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        labels: list[str] | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = description
        if labels:
            fields["labels"] = labels
        if extra_fields:
            fields.update(extra_fields)

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.base_url}/rest/api/2/issue",
                headers=self._headers,
                json={"fields": fields},
            )
            resp.raise_for_status()
            return resp.json()

    async def link_issues(self, from_key: str, to_key: str, link_type: str = "Tests") -> None:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.base_url}/rest/api/2/issueLink",
                headers=self._headers,
                json={
                    "type": {"name": link_type},
                    "inwardIssue": {"key": from_key},
                    "outwardIssue": {"key": to_key},
                },
            )
            resp.raise_for_status()

    async def transition_issue(self, issue_key: str, transition_name: str) -> None:
        transitions = await self.get_issue_transitions(issue_key)
        target = next((t for t in transitions if t["name"] == transition_name), None)
        if not target:
            available = [t["name"] for t in transitions]
            raise ValueError(f"Transition '{transition_name}' not found. Available: {available}")

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.base_url}/rest/api/2/issue/{issue_key}/transitions",
                headers=self._headers,
                json={"transition": {"id": target["id"]}},
            )
            resp.raise_for_status()

    async def attach_file(self, issue_key: str, file_path: str) -> dict:
        from pathlib import Path
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {file_path}")

        headers = self._headers.copy()
        headers.pop("Content-Type")
        headers["X-Atlassian-Token"] = "no-check"

        async with httpx.AsyncClient(timeout=60.0, verify=self.ssl_verify) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    f"{self.base_url}/rest/api/2/issue/{issue_key}/attachments",
                    headers=headers,
                    files={"file": (path.name, f, "application/octet-stream")},
                )
            resp.raise_for_status()
            return resp.json()

    async def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.base_url}/rest/api/2/search",
                headers=self._headers,
                json={"jql": jql, "maxResults": max_results},
            )
            resp.raise_for_status()
            return resp.json().get("issues", [])

    async def update_issue(self, issue_key: str, fields: dict) -> None:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.put(
                f"{self.base_url}/rest/api/2/issue/{issue_key}",
                headers=self._headers,
                json={"fields": fields},
            )
            resp.raise_for_status()
