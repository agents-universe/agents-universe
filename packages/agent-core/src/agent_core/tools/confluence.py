"""Confluence tool — fetch pages and page trees for project documentation."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import httpx

from .base import Tool, ToolContext
from ._auth import ToolAuthError, get_token, get_token_optional
from ._http import ensure_http_client
from .shell import redact_secrets

_log = logging.getLogger(__name__)


class ConfluenceTool(Tool):
    name = "confluence"
    prompt_hint = (
        "The only path to Confluence: fetch pages as cleaned text (good for knowledge "
        "extraction), search via CQL, or create/update pages."
    )
    description = (
        "Fetch, create, and update Confluence pages. Returns cleaned text content "
        "suitable for knowledge extraction. Supports batch page fetching, tree traversal, "
        "CQL search, page creation, and page updates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["get_pages", "get_page_tree", "search", "create_page", "update_page"],
            },
            "page_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One or more Confluence page IDs to fetch",
            },
            "root_page_id": {
                "type": "string",
                "description": "Root page ID for tree traversal",
            },
            "include_body": {
                "type": "boolean",
                "default": False,
                "description": "Include page body in tree results (expensive)",
            },
            "max_pages": {
                "type": "integer",
                "default": 100,
                "description": "Maximum pages to return in tree/search",
            },
            "cql": {
                "type": "string",
                "description": "CQL query for search operation",
            },
            "space_key": {
                "type": "string",
                "description": "Confluence space key (for create_page)",
            },
            "parent_page_id": {
                "type": "string",
                "description": "Parent page ID (for create_page)",
            },
            "page_id": {
                "type": "string",
                "description": "Page ID to update (for update_page)",
            },
            "title": {
                "type": "string",
                "description": "Page title (for create_page and update_page)",
            },
            "body": {
                "type": "string",
                "description": "Page body in Confluence storage format or plain text (for create_page and update_page)",
            },
            "body_format": {
                "type": "string",
                "enum": ["storage", "wiki"],
                "default": "storage",
                "description": "Body format: 'storage' (XHTML) or 'wiki' (wiki markup)",
            },
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
            if operation == "get_pages":
                return await self._op_get_pages(params, client)
            elif operation == "get_page_tree":
                return await self._op_get_page_tree(params, client)
            elif operation == "search":
                return await self._op_search(params, client)
            elif operation == "create_page":
                return await self._op_create_page(params, client)
            elif operation == "update_page":
                return await self._op_update_page(params, client)
            return {"error": f"Unknown operation: {operation}"}
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else ""
            _log.warning("confluence %s HTTP %d: %s", operation, e.response.status_code, body[:200])
            # Atlassian can echo the credential in error bodies — scrub the
            # resolved token and email before the body reaches the LLM/history
            # (same pattern as kong.py / api_request.py), then truncate.
            body = redact_secrets(
                body,
                {"confluence": client.api_token, "jira:email": client.email},
            )[:500]
            return {"error": f"Confluence API returned {e.response.status_code}: {body}"}
        except Exception as e:
            _log.warning("confluence %s failed: %s", operation, e, exc_info=True)
            return {"error": f"Confluence operation failed ({type(e).__name__}): {e}"}

    async def _build_client(self, context: ToolContext) -> "_ConfluenceClient":
        token = await get_token_optional(context, "confluence")
        if not token:
            try:
                token = await get_token(context, "jira")
            except ToolAuthError as orig:
                # Confluence shares Atlassian credentials with Jira — report under
                # 'confluence' so the error message isn't misleading to the user
                msg = str(orig)
                detail = msg.split(" (", 1)[1].rstrip(")") if " (" in msg else ""
                raise ToolAuthError("confluence", detail) from orig
        email = await get_token_optional(context, "jira:email") or ""
        base_url = context.cfg("CONFLUENCE_BASE_URL") or context.cfg("ATLASSIAN_BASE_URL")
        if not base_url:
            raise ToolAuthError("confluence", "ATLASSIAN_BASE_URL is not configured — set it in Settings → Integrations → Jira/Confluence")
        conf_path = context.cfg("CONFLUENCE_BASE_PATH")
        auth_type = context.cfg("ATLASSIAN_AUTH_TYPE", "basic")
        http = ensure_http_client(context, target_url=base_url)
        return _ConfluenceClient(
            api_token=token, email=email,
            base_url=base_url, conf_path=conf_path,
            auth_type=auth_type, http=http,
        )

    async def _op_get_pages(self, params: dict, client: "_ConfluenceClient") -> dict:
        page_ids = params.get("page_ids", [])
        # Same LLM-stringified-param defense as max_pages above: a bare
        # string would be iterated character-by-character below, issuing one
        # fetch per character instead of per page.
        if isinstance(page_ids, str):
            page_ids = [p.strip() for p in page_ids.split(",") if p.strip()]
        if not page_ids:
            return {"error": "page_ids is required (array of page IDs)"}
        pages = []
        for pid in page_ids:
            page = await client.get_page(pid)
            if "error" in page:
                pages.append({"id": pid, "error": page["error"]})
            else:
                pages.append(page)
        return {"pages": pages, "count": len(pages)}

    async def _op_get_page_tree(self, params: dict, client: "_ConfluenceClient") -> dict:
        root_id = params.get("root_page_id", "")
        if not root_id:
            return {"error": "root_page_id is required"}
        # LLM 常把数字参数传成字符串——str 与 int 比较（while len < max_pages）会抛 TypeError。
        try:
            max_pages = int(params.get("max_pages", 100))
        except (TypeError, ValueError):
            max_pages = 100
        include_body = params.get("include_body", False)
        tree = await client.get_page_tree(root_id, max_pages=max_pages)
        if include_body:
            for entry in tree[:max_pages]:
                page = await client.get_page(entry["id"])
                entry["body"] = page.get("body", "")
        return {"root_page_id": root_id, "pages": tree, "count": len(tree)}

    async def _op_search(self, params: dict, client: "_ConfluenceClient") -> dict:
        cql = params.get("cql", "")
        if not cql:
            return {"error": "cql is required"}
        max_pages = params.get("max_pages", 25)
        results = await client.search_cql(cql, limit=max_pages)
        return {"results": results, "count": len(results)}

    async def _op_create_page(self, params: dict, client: "_ConfluenceClient") -> dict:
        title = params.get("title", "")
        body = params.get("body", "")
        space_key = params.get("space_key", "")
        if not title or not space_key:
            return {"error": "title and space_key are required for create_page"}
        payload: dict = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                params.get("body_format", "storage"): {
                    "value": body,
                    "representation": params.get("body_format", "storage"),
                }
            },
        }
        if params.get("parent_page_id"):
            payload["ancestors"] = [{"id": params["parent_page_id"]}]
        resp = await client._http.post(
            f"{client._rest_base}/content",
            headers=client._headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "id": data.get("id"),
            "title": data.get("title"),
            "url": data.get("_links", {}).get("webui", ""),
        }

    async def _op_update_page(self, params: dict, client: "_ConfluenceClient") -> dict:
        page_id = params.get("page_id", "")
        title = params.get("title", "")
        body = params.get("body", "")
        if not page_id or not title:
            return {"error": "page_id and title are required for update_page"}

        # Fetch current version number
        resp = await client._http.get(
            f"{client._rest_base}/content/{page_id}",
            headers=client._headers,
            params={"expand": "version"},
        )
        resp.raise_for_status()
        current_version = resp.json().get("version", {}).get("number", 1)

        payload = {
            "type": "page",
            "title": title,
            "version": {"number": current_version + 1},
            "body": {
                params.get("body_format", "storage"): {
                    "value": body,
                    "representation": params.get("body_format", "storage"),
                }
            },
        }
        resp = await client._http.put(
            f"{client._rest_base}/content/{page_id}",
            headers=client._headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": True,
            "id": data.get("id"),
            "title": data.get("title"),
            "version": data.get("version", {}).get("number"),
            "url": data.get("_links", {}).get("webui", ""),
        }


class _ConfluenceClient:
    """Lightweight Confluence client using shared httpx."""

    def __init__(self, api_token: str, email: str, base_url: str,
                 conf_path: str, auth_type: str, http: httpx.AsyncClient):
        base = base_url.rstrip("/")
        path = conf_path.rstrip("/") if conf_path else ""
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
        return {"Authorization": auth, "Accept": "application/json"}

    @property
    def _rest_base(self) -> str:
        return f"{self.base_url}/rest/api"

    async def get_page(self, page_id: str) -> dict:
        # expand=body.storage pulls the FULL page HTML with no size
        # guard — a huge page ballooned the LLM context / event loop. Stream
        # with a byte cap (mirrors api_request's streaming cap): a plain
        # request() buffers the whole page before any size check could run.
        _MAX_PAGE_BYTES = 5 * 1024 * 1024
        try:
            async with self._http.stream(
                "GET",
                f"{self._rest_base}/content/{page_id}",
                headers=self._headers,
                params={"expand": "body.storage,version,ancestors"},
            ) as resp:
                if resp.status_code >= 400:
                    return {"id": page_id, "error": f"HTTP {resp.status_code}"}
                cl = resp.headers.get("content-length", "")
                if cl.isdigit() and int(cl) > _MAX_PAGE_BYTES:
                    return {"id": page_id, "error": f"Page too large ({cl} bytes > 5MB)"}
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_PAGE_BYTES:
                        return {"id": page_id, "error": "Page too large (> 5MB)"}
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
        except httpx.TimeoutException:
            return {"id": page_id, "error": "Request timed out"}
        except Exception:
            return {"id": page_id, "error": "Confluence request failed"}
        body_html = data.get("body", {}).get("storage", {}).get("value", "")
        if len(body_html) > _MAX_PAGE_BYTES:
            return {"id": page_id, "error": "Page body too large (> 5MB)"}
        ancestors = [a.get("title", "") for a in data.get("ancestors", [])]
        return {
            "id": data.get("id"),
            "title": data.get("title", ""),
            "body": _fix_mojibake(_html_to_text(body_html)),
            "ancestors": ancestors,
            "version": data.get("version", {}).get("number"),
            "url": data.get("_links", {}).get("webui", ""),
        }

    async def get_page_tree(self, root_page_id: str, max_pages: int = 100) -> list[dict]:
        pages: list[dict] = []
        start = 0
        while len(pages) < max_pages:
            resp = await self._http.get(
                f"{self._rest_base}/content/search",
                headers=self._headers,
                params={"cql": f"ancestor={root_page_id}", "limit": min(25, max_pages - len(pages)), "start": start},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for p in results:
                pages.append({"id": p["id"], "title": p["title"], "type": p.get("type", "page")})
            start += len(results)
            # totalSize may be absent from the search response — only stop
            # early when it is actually present (start >= totalSize would
            # always break on the first page for older Confluence servers).
            total_size = data.get("totalSize")
            if total_size is not None and start >= total_size:
                break
        return pages

    async def search_cql(self, cql: str, limit: int = 25) -> list[dict]:
        resp = await self._http.get(
            f"{self._rest_base}/content/search",
            headers=self._headers,
            params={"cql": cql, "limit": limit},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [{"id": p["id"], "title": p["title"], "type": p.get("type", "page")} for p in results]


def _html_to_text(html: str) -> str:
    text = html
    text = re.sub(r'<h([1-6])[^>]*>(.*?)</h\1>', lambda m: '#' * int(m.group(1)) + ' ' + m.group(2) + '\n', text, flags=re.DOTALL)
    text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _fix_mojibake(text: str) -> str:
    try:
        fixed = text.encode('latin1').decode('utf-8')
        if any('一' <= c <= '鿿' for c in fixed):
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return text
