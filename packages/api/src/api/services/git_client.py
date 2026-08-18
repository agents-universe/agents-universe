"""GitHub / GitHub Enterprise REST API client.

Used by git-pr-manager, git-repo-reader, and pr-review-manager skills.
Base URL from system config; token from user DB.
Never logs or returns tokens.
"""
from __future__ import annotations

from typing import Any

import httpx

from api.config import get_settings


class GitClient:
    """Async GitHub Enterprise API client."""

    def __init__(self, token: str, base_url: str = "", api_base_path: str = ""):
        settings = get_settings()
        self.base_url = (base_url or settings.git_base_url).rstrip("/")
        self.api_base_path = api_base_path or settings.git_api_base_path
        self.token = token
        self.timeout = 30.0
        # internal endpoints (Jira Server, Gitea, Confluence)
        # frequently use self-signed certs; the verify flag was configured in
        # settings but never passed to the per-call clients.
        self.ssl_verify = settings.git_ssl_verify

    @property
    def api_url(self) -> str:
        if self.base_url == "https://github.com":
            return "https://api.github.com"
        return f"{self.base_url}{self.api_base_path}"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    async def get_user(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(f"{self.api_url}/user", headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    async def list_prs(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        author: str | None = None,
        reviewer: str | None = None,
        base: str | None = None,
        head: str | None = None,
        per_page: int = 30,
    ) -> list[dict]:
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if base:
            params["base"] = base
        if head:
            params["head"] = head

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/pulls",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            prs = resp.json()

        if author:
            prs = [pr for pr in prs if pr.get("user", {}).get("login", "").lower() == author.lower()]
        if reviewer:
            prs = [
                pr for pr in prs
                if any(r.get("login", "").lower() == reviewer.lower() for r in pr.get("requested_reviewers", []))
            ]

        return prs

    async def get_pr(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/pulls/{number}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_pr_files(self, owner: str, repo: str, number: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/pulls/{number}/files",
                headers=self._headers,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_pr_commits(self, owner: str, repo: str, number: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/pulls/{number}/commits",
                headers=self._headers,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_commit_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/commits/{sha}/status",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_check_runs(self, owner: str, repo: str, sha: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=self._headers,
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return resp.json().get("check_runs", [])

    async def approve_pr(self, owner: str, repo: str, number: int, body: str = "LGTM") -> dict:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(
                f"{self.api_url}/repos/{owner}/{repo}/pulls/{number}/reviews",
                headers=self._headers,
                json={"event": "APPROVE", "body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def merge_pr(
        self,
        owner: str,
        repo: str,
        number: int,
        method: str = "squash",
        commit_title: str = "",
        sha: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"merge_method": method}
        if commit_title:
            payload["commit_title"] = commit_title
        if sha:
            payload["sha"] = sha

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.put(
                f"{self.api_url}/repos/{owner}/{repo}/pulls/{number}/merge",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def search_prs_cross_repo(self, reviewer: str | None = None, author: str | None = None, state: str = "open") -> list[dict]:
        q_parts = [f"is:{state}", "is:pr", "archived:false"]
        if reviewer:
            q_parts.append(f"review-requested:{reviewer}")
        if author:
            q_parts.append(f"author:{author}")

        query = " ".join(q_parts)

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self.api_url}/search/issues",
                headers=self._headers,
                params={"q": query, "per_page": 30},
            )
            resp.raise_for_status()
            return resp.json().get("items", [])
