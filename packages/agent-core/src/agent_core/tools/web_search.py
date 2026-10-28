"""Web search tool — free search by scraping search-engine result pages.

No API key: queries Bing (RSS), Baidu (HTML), or DuckDuckGo (HTML) directly
and parses the result markup with stdlib. Engines are tried in a fallback
chain (WEB_SEARCH_ENGINES env overrides the order); a blocked or unreachable
engine silently falls through to the next, and only an all-fail run surfaces
an error with per-engine reasons.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit

import httpx

from ._http import ensure_http_client, validate_outbound_url
from ._ssrf import SSRFError, validate_redirect, validate_url
from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
# Search pages are tiny; a 2MB cap is plenty and keeps a hostile/broken
# endpoint from streaming a huge body into memory.
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 3
_MAX_QUERY_CHARS = 512
_DEFAULT_CHAIN = ("bing", "baidu", "duckduckgo")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class _SearchError(Exception):
    """Engine-level failure — blocked, unreachable, or unparseable page."""


class _TextStripper(HTMLParser):
    """Collapse an HTML fragment to its text (for Bing RSS descriptions)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(raw: str) -> str:
    stripper = _TextStripper()
    try:
        stripper.feed(raw)
    except Exception:  # noqa: BLE001 — malformed markup must not kill the parse
        return raw
    return stripper.get_text()


class _BaiduParser(HTMLParser):
    """Baidu SERP parser: one result per ``h3 > a``; URL prefers the ``mu``
    attribute (the real destination) over the tracked ``href``.

    Snippet class names carry hashes that churn, so instead of matching them
    the first >=30-char text block appearing after a result's title is taken
    as its snippet.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_h3 = False
        self._in_link = False
        self._current: dict[str, str] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if tag == "h3":
            self._in_h3 = True
        elif tag == "a" and self._in_h3:
            self._in_link = True
            self._current = {
                "title": "",
                "url": attrs_d.get("mu") or attrs_d.get("href") or "",
                "snippet": "",
            }
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._buf.append(data)
            return
        text = " ".join(data.split())
        if text and self.results and not self.results[-1]["snippet"] and len(text) >= 30:
            self.results[-1]["snippet"] = text

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link and self._current is not None:
            self._current["title"] = " ".join(" ".join(self._buf).split())
            if self._current["url"] and self._current["title"]:
                self.results.append(self._current)
            self._in_link = False
            self._current = None
            self._buf = []
        elif tag == "h3":
            self._in_h3 = False


def _ddg_real_url(href: str) -> str:
    """Unwrap DuckDuckGo's ``/l/?uddg=<urlencoded>`` redirect to the target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return target
    if href.startswith(("http://", "https://")):
        return href
    return ""


class _DdgParser(HTMLParser):
    """DuckDuckGo HTML parser: ``result__a`` anchors open a result (title +
    real URL from the uddg param), ``result__snippet`` anchors set its snippet."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._capture: str | None = None  # "title" | "snippet"
        self._url = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        if "result__a" in classes:
            self._flush()
            self._capture = "title"
            self._url = _ddg_real_url(dict(attrs).get("href") or "")
            self._buf = []
        elif "result__snippet" in classes:
            self._flush()
            self._capture = "snippet"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture:
            self._flush()

    def _flush(self) -> None:
        text = " ".join(" ".join(self._buf).split())
        if self._capture == "title":
            if text and self._url:
                self.results.append({"title": text, "url": self._url, "snippet": ""})
        elif self._capture == "snippet" and text and self.results:
            self.results[-1]["snippet"] = text
        self._capture = None
        self._buf = []


def _parse_bing_rss(text: str, num: int) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise _SearchError(f"unexpected page format (not RSS): {e}") from e
    results: list[dict[str, str]] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        results.append({
            "title": (item.findtext("title") or "").strip(),
            "url": link,
            "snippet": _strip_html(item.findtext("description") or ""),
        })
        if len(results) >= num:
            break
    return results


async def _fetch_page(url: str, context: ToolContext) -> tuple[int, str, str]:
    """GET *url* with the shared client (proxy-aware); returns (status, text,
    final_url). Manual redirect loop, each hop SSRF re-validated."""
    try:
        validate_url(url)
        validate_outbound_url(url)
    except SSRFError as e:
        raise _SearchError(f"blocked by SSRF policy: {e}") from e

    http = ensure_http_client(context, target_url=url)
    current_url = url
    redirects = 0

    try:
        while True:
            async with http.stream(
                "GET",
                current_url,
                timeout=_TIMEOUT_SECONDS,
                headers=_HEADERS,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        raise _SearchError(f"too many redirects (max {_MAX_REDIRECTS})")
                    location = response.headers.get("location", "")
                    if not location:
                        raise _SearchError("redirect with no Location header")
                    resolved = urljoin(current_url, location)
                    if urlparse(resolved).scheme not in ("http", "https"):
                        raise _SearchError(f"redirect to non-http URL: {location}")
                    try:
                        validate_redirect(resolved)
                        validate_outbound_url(resolved)
                    except SSRFError as e:
                        raise _SearchError(f"redirect blocked by SSRF policy: {e}") from e
                    current_url = resolved
                    continue

                total = 0
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_PAGE_BYTES:
                        raise _SearchError(f"response too large (exceeded {_MAX_PAGE_BYTES} bytes)")
                    chunks.append(chunk)
                text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return response.status_code, text, current_url
    except httpx.TimeoutException:
        raise _SearchError(f"timed out after {_TIMEOUT_SECONDS}s") from None
    except httpx.RequestError as e:
        raise _SearchError(f"request failed: {e}") from e


async def _search_bing(query: str, num: int, context: ToolContext) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
    status, text, _final = await _fetch_page(url, context)
    if status >= 400:
        raise _SearchError(f"HTTP {status}")
    results = _parse_bing_rss(text, num)
    if not results and "challenge" in text.lower():
        raise _SearchError("blocked by challenge page")
    return results


async def _search_baidu(query: str, num: int, context: ToolContext) -> list[dict[str, str]]:
    url = "https://www.baidu.com/s?" + urlencode({"wd": query, "ie": "utf-8", "rn": "10"})
    status, text, final_url = await _fetch_page(url, context)
    if "wappass.baidu.com" in final_url:
        raise _SearchError("blocked by captcha (wappass redirect)")
    if status >= 400:
        raise _SearchError(f"HTTP {status}")
    if "安全验证" in text or "请输入验证码" in text:
        raise _SearchError("blocked by captcha page")
    parser = _BaiduParser()
    try:
        parser.feed(text)
    except Exception as e:  # noqa: BLE001 — parser bugs become engine failures
        raise _SearchError(f"parse error: {e}") from e
    return parser.results[:num]


async def _search_duckduckgo(query: str, num: int, context: ToolContext) -> list[dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    status, text, final_url = await _fetch_page(url, context)
    if "anomaly.duckduckgo.com" in final_url:
        raise _SearchError("blocked by anomaly check")
    if status >= 400:
        raise _SearchError(f"HTTP {status}")
    parser = _DdgParser()
    try:
        parser.feed(text)
    except Exception as e:  # noqa: BLE001
        raise _SearchError(f"parse error: {e}") from e
    return parser.results[:num]


_ENGINE_FUNCTIONS = {
    "bing": _search_bing,
    "baidu": _search_baidu,
    "duckduckgo": _search_duckduckgo,
}


def _resolve_chain(engine: str, context: ToolContext) -> list[str]:
    if engine != "auto":
        return [engine]
    raw = context.cfg("WEB_SEARCH_ENGINES", "")
    names = [n.strip().lower() for n in raw.split(",")] if raw else []
    chain = [n for n in names if n in _ENGINE_FUNCTIONS]
    return chain or list(_DEFAULT_CHAIN)


class WebSearchTool(Tool):
    name = "web_search"
    prompt_hint = (
        "Free web search (no API key) by scraping Bing/Baidu/DuckDuckGo result "
        "pages. Use for current info, market research, and docs discovery; "
        "follow up with web_fetch to read promising URLs and verify anything "
        "critical. Engines fail over automatically — if all fail, retry later "
        "or pin a single engine."
    )
    description = "Search the web via search engine result pages (Bing/Baidu/DuckDuckGo; no API key)."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords"},
            "num_results": {"type": "integer", "default": 8, "description": "Max results (1-10)"},
            "engine": {
                "type": "string",
                "enum": ["auto", "bing", "baidu", "duckduckgo"],
                "default": "auto",
                "description": "Pin a specific engine or use the fallback chain",
            },
        },
        "required": ["query"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            return {"error": "query is required and must be a non-empty string"}
        query = query.strip()[:_MAX_QUERY_CHARS]

        try:
            num_results = int(params.get("num_results", 8))
        except (TypeError, ValueError):
            num_results = 8
        num_results = max(1, min(num_results, 10))

        engine = params.get("engine", "auto")
        if not isinstance(engine, str) or engine not in ("auto", *_ENGINE_FUNCTIONS):
            return {"error": f"Unknown engine: {engine!r}; use auto/bing/baidu/duckduckgo"}

        tried: list[dict[str, str]] = []
        for name in _resolve_chain(engine, context):
            try:
                results = await _ENGINE_FUNCTIONS[name](query, num_results, context)
            except _SearchError as e:
                _log.warning("web_search engine %s failed: %s", name, e)
                tried.append({"engine": name, "reason": str(e)})
                continue
            return {
                "results": results,
                "engine": name,
                "query": query,
                "count": len(results),
            }

        return {
            "error": "All search engines failed",
            "engines_tried": tried,
            "hint": (
                "Each engine's reason is in engines_tried (timeout = unreachable, "
                "captcha/challenge = blocked). Set WEB_SEARCH_ENGINES=bing,baidu to "
                "skip unreachable engines, or configure a proxy for DuckDuckGo."
            ),
        }
