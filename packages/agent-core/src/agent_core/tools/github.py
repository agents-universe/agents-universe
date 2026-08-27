"""GitHub tool (public GitHub and GitHub Enterprise) — PR management, commit search, repository info."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx

from .base import Tool, ToolContext
from ._auth import ToolAuthError, get_token
from ._http import ensure_http_client
from .shell import redact_secrets

_log = logging.getLogger(__name__)


class GitHubTool(Tool):
    name = "github"
    prompt_hint = (
        "First stop for any PR task — the remote PR diff, reviews, comments, and "
        "checks are authoritative. Use for GitHub work (public GitHub or GitHub "
        "Enterprise): search commits/PRs by Jira key, review, approve, merge, or "
        "create PRs, fork/star repositories, and check CI statuses."
    )
    description = (
        "GitHub (public or Enterprise): search commits/PRs by Jira key, "
        "list/detail/approve/merge/create PRs, "
        "fork a repository, star/check-star a repository, get repository and user info, "
        "get commit check-runs and combined status."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "search_by_jira_key", "get_repo_info", "get_user",
                    "list_prs", "get_pr_detail", "approve_pr", "merge_pr", "create_pr",
                    "get_commit_checks", "add_pr_comment", "fork", "is_starred", "star",
                ],
            },
            "jira_key": {"type": "string", "description": "Jira issue key to search for"},
            "repository": {"type": "string", "description": "owner/repo format"},
            "number": {"type": "integer", "description": "PR number"},
            "url": {"type": "string", "description": "Full PR URL (alternative to repository+number)"},
            "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
            "author": {"type": "string"},
            "reviewer": {"type": "string"},
            "base": {"type": "string", "description": "Base branch filter for list_prs"},
            "head": {"type": "string", "description": "Head branch filter for list_prs"},
            "head_branch": {"type": "string", "description": "Feature branch to open PR from (for create_pr). For a cross-repo (fork) PR use '<fork-owner>:<branch>'; for a same-repo PR use the bare branch name."},
            "base_branch": {"type": "string", "description": "Target branch for the new PR (for create_pr), defaults to main"},
            "title": {"type": "string", "description": "PR title (for create_pr)"},
            "draft": {"type": "boolean", "default": False, "description": "Open as draft PR (for create_pr)"},
            "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"], "default": "squash"},
            "body": {"type": "string", "description": "PR/review/merge comment body"},
            "commit_title": {"type": "string"},
            "sha": {"type": "string", "description": "Head SHA for merge verification"},
            "all_repos": {"type": "boolean", "default": False, "description": "Search across all repos"},
            "limit": {"type": "integer", "default": 30},
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]

        try:
            token = await get_token(context, "git")
        except ToolAuthError as e:
            return {"error": str(e)}

        base_url = context.cfg("GIT_BASE_URL").rstrip("/")
        if not base_url:
            return {"error": "GIT_BASE_URL is not configured — set it in Settings → Integrations → Git"}
        api_path = context.cfg("GIT_API_BASE_PATH", "/api/v3")
        if base_url in ("https://github.com", "http://github.com", "www.github.com", "github.com"):
            # Public GitHub: the API lives at api.github.com and paths are
            # relative to it — no /api/v3 prefix, unlike GitHub Enterprise.
            base_url = "https://api.github.com"
            api_path = ""
        api_url = f"{base_url}{api_path}"
        http = ensure_http_client(context, target_url=base_url)
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

        try:
            handler = getattr(self, f"_op_{operation}", None)
            if not handler:
                return {"error": f"Unknown operation: {operation}"}
            return await handler(params, api_url, headers, http)
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            status = e.response.status_code
            _log.warning("github %s HTTP %d: %s", operation, status, body[:200])
            # Gateways echo the submitted credential back in error bodies
            # ("Bad credentials: <token>") — the same pattern kong.py
            # redacts. Scrub the resolved token before the body reaches the
            # LLM/history, then truncate so a masked value is never cut off.
            body = redact_secrets(body, {"git": token})[:500]
            hint = ""
            if status == 403:
                hint = (
                    " The Git token's account lacks permission for this repository — "
                    "check that the personal access token has write scope "
                    "(classic PAT: 'repo'; fine-grained: Contents + Pull requests read-and-write) "
                    "and that the account is a collaborator with write access."
                )
            return {"error": f"GitHub API returned {status}: {body}{hint}"}
        except Exception as e:
            _log.warning("github %s failed: %s", operation, e, exc_info=True)
            return {"error": f"GitHub operation failed: {e}"}

    async def _op_search_by_jira_key(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        jira_key = params.get("jira_key", "")
        if not jira_key:
            return {"error": "jira_key is required"}
        resp = await http.get(
            f"{api_url}/search/issues",
            headers=headers,
            params={"q": f"{jira_key} is:pr", "per_page": 20},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        prs = [{"number": i["number"], "title": i["title"], "state": i["state"],
                "repository": i.get("repository_url", "").split("/repos/")[-1],
                "url": i.get("html_url", "")} for i in items]

        resp2 = await http.get(
            f"{api_url}/search/commits",
            headers={**headers, "Accept": "application/vnd.github.cloak-preview+json"},
            params={"q": jira_key, "per_page": 10},
        )
        commits = []
        if resp2.status_code == 200:
            for c in resp2.json().get("items", []):
                commits.append({"sha": c["sha"][:8], "message": c["commit"]["message"][:120],
                                "repository": c.get("repository", {}).get("full_name", "")})

        return {"jira_key": jira_key, "pull_requests": prs, "commits": commits}

    async def _op_get_repo_info(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (owner/repo format)"}
        resp = await http.get(f"{api_url}/repos/{repo}", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return {"full_name": data["full_name"], "default_branch": data["default_branch"],
                "description": data.get("description", ""), "private": data.get("private", True)}

    async def _op_get_user(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        resp = await http.get(f"{api_url}/user", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return {"login": data["login"], "name": data.get("name", ""), "email": data.get("email", "")}

    async def _op_list_prs(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        all_repos = params.get("all_repos", False)
        if all_repos:
            author = params.get("author")
            reviewer = params.get("reviewer")
            state = params.get("state", "open")
            q_parts = [f"is:{state}", "is:pr", "archived:false"]
            if reviewer:
                q_parts.append(f"review-requested:{reviewer}")
            if author:
                q_parts.append(f"author:{author}")
            resp = await http.get(f"{api_url}/search/issues", headers=headers,
                                  params={"q": " ".join(q_parts), "per_page": params.get("limit", 30)})
            resp.raise_for_status()
            items = resp.json().get("items", [])
            return {"prs": [{"number": i["number"], "title": i["title"], "state": i["state"],
                             "repository": i.get("repository_url", "").split("/repos/")[-1],
                             "url": i.get("html_url", "")} for i in items], "count": len(items)}

        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (or set all_repos=true)"}
        query_params: dict[str, Any] = {"state": params.get("state", "open"), "per_page": params.get("limit", 30)}
        if params.get("base"):
            query_params["base"] = params["base"]
        if params.get("head"):
            query_params["head"] = params["head"]
        resp = await http.get(f"{api_url}/repos/{repo}/pulls", headers=headers, params=query_params)
        resp.raise_for_status()
        prs = resp.json()
        if params.get("author"):
            prs = [p for p in prs if p.get("user", {}).get("login", "").lower() == params["author"].lower()]
        if params.get("reviewer"):
            rev = params["reviewer"].lower()
            prs = [p for p in prs if any(r.get("login", "").lower() == rev for r in p.get("requested_reviewers", []))]
        return {"prs": [{"number": p["number"], "title": p["title"], "state": p["state"],
                         "author": p.get("user", {}).get("login", ""),
                         "url": p.get("html_url", "")} for p in prs], "count": len(prs)}

    async def _op_get_pr_detail(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo, number = self._resolve_pr(params)
        if not repo or not number:
            return {"error": "repository+number or url is required"}

        pr_resp, files_resp, commits_resp = await asyncio.gather(
            http.get(f"{api_url}/repos/{repo}/pulls/{number}", headers=headers),
            http.get(f"{api_url}/repos/{repo}/pulls/{number}/files", headers=headers, params={"per_page": 100}),
            http.get(f"{api_url}/repos/{repo}/pulls/{number}/commits", headers=headers, params={"per_page": 100}),
        )
        pr_resp.raise_for_status()
        pr = pr_resp.json()

        files = files_resp.json() if files_resp.status_code == 200 else []
        commits = commits_resp.json() if commits_resp.status_code == 200 else []

        checks = []
        head_sha = pr.get("head", {}).get("sha", "")
        if head_sha:
            cr_resp = await http.get(f"{api_url}/repos/{repo}/commits/{head_sha}/check-runs",
                                     headers=headers, params={"per_page": 50})
            if cr_resp.status_code == 200:
                checks = [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")}
                          for c in cr_resp.json().get("check_runs", [])]

        return {
            "number": pr["number"], "title": pr["title"], "state": pr["state"],
            "draft": pr.get("draft", False),
            "author": pr.get("user", {}).get("login", ""),
            "body": (pr.get("body") or "")[:3000],
            "base": pr.get("base", {}).get("ref", ""),
            "head": pr.get("head", {}).get("ref", ""),
            "head_sha": head_sha,
            "mergeable": pr.get("mergeable"),
            "files": [{"filename": f["filename"], "status": f["status"],
                       "additions": f["additions"], "deletions": f["deletions"]} for f in files[:50]],
            "commits": [{"sha": c["sha"][:8], "message": c["commit"]["message"][:120]} for c in commits],
            "checks": checks,
        }

    async def _op_approve_pr(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo, number = self._resolve_pr(params)
        if not repo or not number:
            return {"error": "repository+number or url is required"}
        body = params.get("body", "LGTM")
        resp = await http.post(f"{api_url}/repos/{repo}/pulls/{number}/reviews",
                               headers=headers, json={"event": "APPROVE", "body": body})
        resp.raise_for_status()
        return {"success": True, "repository": repo, "number": number, "action": "approved"}

    async def _op_merge_pr(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo, number = self._resolve_pr(params)
        if not repo or not number:
            return {"error": "repository+number or url is required"}
        payload: dict[str, Any] = {"merge_method": params.get("merge_method", "squash")}
        if params.get("commit_title"):
            payload["commit_title"] = params["commit_title"]
        if params.get("sha"):
            payload["sha"] = params["sha"]
        resp = await http.put(f"{api_url}/repos/{repo}/pulls/{number}/merge",
                              headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {"success": True, "merged": data.get("merged", True),
                "sha": data.get("sha", ""), "message": data.get("message", "")}

    async def _op_get_commit_checks(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo = params.get("repository", "")
        sha = params.get("sha", "")
        if not repo or not sha:
            return {"error": "repository and sha are required"}

        check_runs_resp, status_resp = await asyncio.gather(
            http.get(f"{api_url}/repos/{repo}/commits/{sha}/check-runs",
                     headers=headers, params={"per_page": 100}),
            http.get(f"{api_url}/repos/{repo}/commits/{sha}/status", headers=headers),
        )

        check_runs = []
        if check_runs_resp.status_code == 200:
            for c in check_runs_resp.json().get("check_runs", []):
                check_runs.append({
                    "name": c["name"],
                    "status": c["status"],
                    "conclusion": c.get("conclusion"),
                    "url": c.get("html_url", ""),
                })

        combined_state = None
        statuses = []
        if status_resp.status_code == 200:
            data = status_resp.json()
            combined_state = data.get("state")
            for s in data.get("statuses", []):
                statuses.append({
                    "context": s.get("context"),
                    "state": s.get("state"),
                    "description": s.get("description", ""),
                })

        # queued/waiting/pending check-runs have conclusion=None
        # and were previously counted as passing — the agent would merge a PR
        # whose CI hadn't even started. A check passes only when it has
        # COMPLETED with a success/skipped conclusion; a failed statuses API
        # call (combined_state None) must not read as passing either.
        all_passing = (
            combined_state == "success"
            and all(
                c["status"] == "completed" and c["conclusion"] in ("success", "skipped")
                for c in check_runs
            )
        )

        return {
            "repository": repo,
            "sha": sha,
            "combined_state": combined_state,
            "all_passing": all_passing,
            "check_runs": check_runs,
            "statuses": statuses,
        }

    async def _op_add_pr_comment(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo, number = self._resolve_pr(params)
        if not repo or not number:
            return {"error": "repository+number or url is required"}
        body = params.get("body", "")
        if not body:
            return {"error": "body is required"}
        resp = await http.post(f"{api_url}/repos/{repo}/issues/{number}/comments",
                               headers=headers, json={"body": body})
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "repository": repo,
            "number": number,
            "comment_id": data.get("id"),
            "comment_url": data.get("html_url", ""),
        }

    async def _op_create_pr(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (owner/repo format)"}
        if not params.get("title") or not params.get("head_branch"):
            return {"error": "title and head_branch are required"}
        payload: dict[str, Any] = {
            "title": params["title"],
            "head": params["head_branch"],
            "base": params.get("base_branch", "main"),
            "body": params.get("body", ""),
            "draft": params.get("draft", False),
        }
        resp = await http.post(f"{api_url}/repos/{repo}/pulls", headers=headers, json=payload)
        if resp.status_code == 422:
            # GitHub returns 422 for both duplicate-PR and invalid-request cases.
            # Query the exact owner-qualified head and base before classifying it.
            # A fork PR head is already '<fork-owner>:<branch>'; a same-repo head
            # is qualified with the upstream owner.
            head_branch = params["head_branch"]
            qualified_head = (
                head_branch if ":" in head_branch
                else f"{repo.split('/', 1)[0]}:{head_branch}"
            )
            existing_resp = await http.get(
                f"{api_url}/repos/{repo}/pulls",
                headers=headers,
                params={
                    "state": "open",
                    "head": qualified_head,
                    "base": payload["base"],
                    "per_page": 100,
                },
            )
            existing_resp.raise_for_status()
            existing = existing_resp.json()
            if len(existing) == 1:
                data = existing[0]
                return {
                    "status": "already_exists",
                    "number": data["number"],
                    "url": data["html_url"],
                    "title": data.get("title", ""),
                    "head": data.get("head", {}).get("ref", params["head_branch"]),
                    "base": data.get("base", {}).get("ref", payload["base"]),
                }
            if not existing:
                return {
                    "error": (
                        "GitHub create_pr returned 422, but no unique open PR was found "
                        f"for {repo}:{params['head_branch']} -> {payload['base']}"
                    )
                }
            return {
                "error": (
                    "GitHub create_pr returned 422 and the owner:head/base query found "
                    f"{len(existing)} open PRs; refusing to choose one"
                )
            }
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "number": data["number"],
            "url": data["html_url"],
            "title": data["title"],
            "head": data["head"]["ref"],
            "base": data["base"]["ref"],
        }

    async def _op_fork(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        """Create a fork of the target repository under the configured Git account.

        POST /repos/{owner}/{repo}/forks is idempotent in effect: if a fork with
        the same name already exists, GitHub returns 202 with the existing fork.
        """
        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (owner/repo format)"}
        resp = await http.post(f"{api_url}/repos/{repo}/forks", headers=headers, json={})
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "forked",
            "full_name": data.get("full_name", ""),
            "clone_url": data.get("clone_url", ""),
            "ssh_url": data.get("ssh_url", ""),
            "html_url": data.get("html_url", ""),
            "default_branch": data.get("default_branch", ""),
            "parent": repo,
        }

    async def _op_is_starred(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        """Check whether the configured Git account has starred the repository."""
        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (owner/repo format)"}
        resp = await http.get(f"{api_url}/user/starred/{repo}", headers=headers)
        if resp.status_code == 204:
            return {"starred": True, "repository": repo}
        if resp.status_code == 404:
            # 404 means "not starred" (the endpoint also 404s for repos the
            # account cannot see, which is equivalent for our purpose).
            return {"starred": False, "repository": repo}
        resp.raise_for_status()
        return {"starred": True, "repository": repo}

    async def _op_star(self, params: dict, api_url: str, headers: dict, http: httpx.AsyncClient) -> dict:
        """Star the repository with the configured Git account (PUT /user/starred/...)."""
        repo = params.get("repository", "")
        if not repo:
            return {"error": "repository is required (owner/repo format)"}
        resp = await http.put(f"{api_url}/user/starred/{repo}", headers=headers)
        if resp.status_code == 204:
            return {"status": "starred", "repository": repo}
        resp.raise_for_status()
        return {"status": "starred", "repository": repo}

    def _resolve_pr(self, params: dict) -> tuple[str, int | None]:
        if params.get("url"):
            m = re.match(r".*/repos?/([^/]+/[^/]+)/pulls?/(\d+)", params["url"])
            if not m:
                m = re.match(r"https?://[^/]+/([^/]+/[^/]+)/pull/(\d+)", params["url"])
            if m:
                return m.group(1), int(m.group(2))
        repo = params.get("repository", "")
        number = params.get("number")
        return repo, number
