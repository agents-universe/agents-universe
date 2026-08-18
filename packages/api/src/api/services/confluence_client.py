"""Confluence REST API client — reads page content and page trees.

Base URL from system config; credentials from user DB.
Never logs or returns credentials.
"""
from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from api.config import get_settings


class ConfluenceClient:
    """Async Confluence API client."""

    def __init__(self, api_token: str, email: str = "", base_url: str = "", auth_type: str = ""):
        settings = get_settings()
        base = (base_url or settings.atlassian_base_url).rstrip("/")
        conf_path = settings.atlassian_confluence_base_path.rstrip("/")
        self.base_url = f"{base}{conf_path}"
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
            "Accept": "application/json",
        }

    @property
    def _rest_base(self) -> str:
        """REST API base path: Server/DC omits /wiki; Cloud uses /wiki."""
        settings = get_settings()
        # If confluence_base_path is set we're on Server/DC — no /wiki prefix
        if settings.atlassian_confluence_base_path:
            return f"{self.base_url}/rest/api"
        return f"{self.base_url}/wiki/rest/api"

    async def get_page(self, page_id: str, expand: str = "body.storage,version") -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.get(
                f"{self._rest_base}/content/{page_id}",
                headers=self._headers,
                params={"expand": expand},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_page_tree(self, root_page_id: str, max_pages: int = 100) -> list[dict]:
        pages = []
        start = 0
        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            while len(pages) < max_pages:
                resp = await client.get(
                    f"{self._rest_base}/content/search",
                    headers=self._headers,
                    params={
                        "cql": f"ancestor={root_page_id}",
                        "limit": min(25, max_pages - len(pages)),
                        "start": start,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                pages.extend([{"id": p["id"], "title": p["title"], "type": p.get("type", "page")} for p in results])
                start += len(results)
                if start >= data.get("totalSize", 0):
                    break

        return pages

    async def get_pages_batch(self, page_ids: list[str]) -> list[dict]:
        results = []
        for page_id in page_ids:
            try:
                page = await self.get_page(page_id)
                results.append(page)
            except httpx.HTTPStatusError:
                results.append({"id": page_id, "error": "fetch failed"})
        return results

    @staticmethod
    def html_to_text(html: str) -> str:
        """Strip HTML tags preserving headings and structure."""
        text = html
        text = re.sub(r'<h([1-6])[^>]*>(.*?)</h\1>', lambda m: '#' * int(m.group(1)) + ' ' + m.group(2) + '\n', text)
        text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', text)
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def fix_mojibake(text: str) -> str:
        """Attempt to fix latin1-encoded UTF-8 text."""
        try:
            fixed = text.encode('latin1').decode('utf-8')
            if any('一' <= c <= '鿿' for c in fixed):
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return text
