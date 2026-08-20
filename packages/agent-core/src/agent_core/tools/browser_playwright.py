"""Playwright browser tool — headless Chromium automation."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext
from ._http import _is_ssrf_enabled, validate_outbound_url
from ._media import media_type_for, sanitize_suffix
from ._ssrf import SSRFError, validate_url

_log = logging.getLogger(__name__)

# single clamp shared by every operation — goto got this first, while
# click/fill/wait_for_selector still let a 10^9 ms "timeout"
# park the tool call for ~11 days (holding the agent loop and, in task
# mode, a semaphore slot).
_MIN_TIMEOUT_MS = 1000
_MAX_TIMEOUT_MS = 120_000


def _clamp_timeout(value: Any) -> int:
    try:
        return min(max(int(value), _MIN_TIMEOUT_MS), _MAX_TIMEOUT_MS)
    except (TypeError, ValueError):
        return 30_000


def _check_browser_url(url: str) -> None:
    """SSRF-validate a URL for the browser tool.

    The literal scheme/host/IP/metadata checks always apply; the PORT
    allowlist and DNS resolution follow the SSRF_ENABLED gate — with SSRF
    disabled a page (or its subresources) on any port must load, or agents
    can never open their own dev frontends on arbitrary Vite/backend ports.
    """
    if _is_ssrf_enabled():
        validate_url(url)
        validate_outbound_url(url)
    else:
        validate_url(url, allow_any_port=True)


class BrowserPlaywrightTool(Tool):
    name = "browser_playwright"
    prompt_hint = (
        "Use when you must interact with a live page — JS-heavy sites, clicks, form "
        "fills, screenshots, file downloads. For plain text retrieval prefer web_fetch; it is cheaper."
    )
    description = (
        "Control a headless Chromium browser. Supports navigation, clicks, form fills, "
        "screenshots, file downloads, and JavaScript evaluation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goto", "click", "fill", "screenshot", "wait_for_selector", "evaluate", "get_text", "download"],
                "description": "Browser operation to perform",
            },
            "url": {"type": "string", "description": "URL for goto operation"},
            "selector": {"type": "string", "description": "CSS selector for element operations"},
            "value": {"type": "string", "description": "Value for fill operation"},
            "script": {"type": "string", "description": "JavaScript expression for evaluate"},
            "full_page": {"type": "boolean", "default": True, "description": "Full page screenshot"},
            "timeout": {"type": "integer", "default": 30000, "description": "Timeout in ms"},
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]

        # The page is a session-scoped resource (like the browser): read and
        # write it through the shared context so a task clone reuses the
        # session's page and cleanup() can close it.
        owner = getattr(context, "_shared", None) or context

        # Initialize browser lazily, sharing the session lifecycle with other tools.
        try:
            browser = await context.ensure_browser()
        except Exception as e:
            msg = repr(e) if not str(e) else str(e)
            return {"error": f"Failed to start browser ({type(e).__name__}): {msg}. Run: playwright install chromium"}

        # Get or create a page; recreate if previous page was closed or crashed
        page = getattr(owner, "_browser_page", None)
        if page is not None:
            try:
                await page.title()
            except Exception:
                try:
                    await page.close()
                except Exception:
                    pass
                page = None
                owner._browser_page = None

        if page is None:
            ignore_https = not getattr(context, "browser_ssl_verify", True)
            page = await browser.new_page(ignore_https_errors=ignore_https)
            owner._browser_page = page
            # SSRF interception is registered ONCE and stays for the
            # page's lifetime. It was first registered inside `goto` and unrouted
            # in finally — a later click (link navigation), form submit, or
            # evaluate("location=...") navigated WITHOUT any check, letting an
            # internal/metadata page be read by get_text/evaluate afterwards.
            # Registered here, every request of every subsequent navigation is
            # validated before it leaves the browser.
            async def _ssrf_route(route, req):
                try:
                    # data:/blob:/about: subresources carry no network
                    # destination — route them through unchanged.
                    scheme = (req.url.split(":", 1)[0] or "").lower()
                    if scheme in ("data", "blob", "about"):
                        await route.continue_()
                        return
                    _check_browser_url(req.url)
                except SSRFError as e:
                    _log.warning("browser blocked SSRF target: %s (%s)", req.url, e)
                    await route.abort()
                    return
                try:
                    await route.continue_()
                except Exception:
                    pass  # navigation may already be torn down

            await page.route("**/*", _ssrf_route)

        try:
            if operation == "goto":
                url = params.get("url", "")
                # SSRF validation: literal checks always; port allowlist + DNS
                # resolution behind the SSRF_ENABLED gate (see _check_browser_url).
                try:
                    _check_browser_url(url)
                except SSRFError as e:
                    return {"error": f"URL blocked by SSRF protection: {e}"}
                failed_requests: list[str] = []

                def _on_request_failed(req):
                    failed_requests.append(f"{req.method} {req.url} — {req.failure}")

                # params.timeout flows straight into Playwright with no
                # upper bound — a 10^9 ms "timeout" parks the tool call for days.
                timeout = _clamp_timeout(params.get("timeout", 30000))

                # redirects could land on an internal address the
                # initial check never saw (302 → http://169.254.169.254) and
                # the post-hoc check ran AFTER the request already went out —
                # leaving the page parked on the internal URL for later
                # get_text/evaluate reads with no further validation. The
                # per-request route registered at page creation aborts SSRF
                # targets before they leave the browser; the post-hoc check
                # below still decides the outcome for redirect chains.

                page.on("requestfailed", _on_request_failed)
                try:
                    _log.info("browser goto: %s (timeout=%s, ignore_https=%s)",
                              url, timeout,
                              not getattr(context, "browser_ssl_verify", True))
                    try:
                        response = await page.goto(url, timeout=timeout)
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception:
                        # An aborted SSRF redirect surfaces here; the post-hoc
                        # check below still decides the outcome.
                        response = None
                    # Redirects can land on a host the caller's URL check never
                    # saw (e.g. 302 → http://169.254.169.254). Re-run the
                    # literal check against the FINAL URL.
                    try:
                        _check_browser_url(page.url)
                    except SSRFError as e:
                        # Close the page so no later operation (get_text,
                        # evaluate, click) can read the internal address.
                        try:
                            await page.close()
                        except Exception:
                            pass
                        owner._browser_page = None
                        return {"error": f"URL blocked by SSRF protection: {e}"}
                finally:
                    page.remove_listener("requestfailed", _on_request_failed)

                result: dict[str, Any] = {
                    "title": await page.title(),
                    "url": page.url,
                    "status": response.status if response else None,
                }
                if failed_requests:
                    _log.warning("browser goto failed requests: %s", failed_requests)
                    result["failed_requests"] = failed_requests
                return result

            elif operation == "click":
                selector = params.get("selector", "")
                await page.click(selector, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"success": True, "selector": selector}

            elif operation == "fill":
                selector = params.get("selector", "")
                value = params.get("value", "")
                await page.fill(selector, value, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"success": True, "selector": selector}

            elif operation == "screenshot":
                media_path = Path(context.conversation_media_dir)
                media_path.mkdir(parents=True, exist_ok=True)
                filename = f"screenshot_{uuid.uuid4().hex[:8]}.png"
                screenshot_path = str(media_path / filename)
                full_page = params.get("full_page", True)
                await page.screenshot(path=screenshot_path, full_page=full_page)
                rel_path = f"/api/media/{context.project_id}/{context.conversation_id}/{filename}"
                return {
                    "screenshot_path": screenshot_path,
                    "url": rel_path,
                    "title": await page.title(),
                    "page_url": page.url,
                    "images": [{"id": filename, "url": rel_path, "alt": f"Screenshot of {page.url}", "path": screenshot_path}],
                }

            elif operation == "wait_for_selector":
                selector = params.get("selector", "")
                await page.wait_for_selector(selector, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"found": True, "selector": selector}

            elif operation == "evaluate":
                script = params.get("script", "")
                result = await page.evaluate(script)
                # evaluate returns whatever the page produces — a
                # `return document.documentElement.outerHTML` script can yield
                # multi-MB payloads straight into the LLM context. Cap it the
                # way get_text does (5000 chars), with a truncated marker.
                try:
                    text = json.dumps(result, ensure_ascii=False, default=str)
                except Exception:
                    text = str(result)
                _MAX_EVALUATE_CHARS = 20_000
                if len(text) > _MAX_EVALUATE_CHARS:
                    return {"result": text[:_MAX_EVALUATE_CHARS], "truncated": True, "total_chars": len(text)}
                return {"result": result}

            elif operation == "get_text":
                selector = params.get("selector", "body")
                text = await page.inner_text(selector)
                return {"text": text[:5000]}

            elif operation == "download":
                selector = params.get("selector", "")
                if not selector:
                    return {"error": "download requires a 'selector' for the element that triggers the download"}
                timeout = _clamp_timeout(params.get("timeout", 30000))
                media_path = Path(context.conversation_media_dir)
                media_path.mkdir(parents=True, exist_ok=True)
                try:
                    async with page.expect_download(timeout=timeout) as download_info:
                        await page.click(selector, timeout=timeout)
                    download = await download_info.value
                except Exception as e:
                    return {"error": f"Download not captured: {e}"}
                suggested = download.suggested_filename
                suffix = sanitize_suffix(suggested)
                dest = media_path / f"download_{uuid.uuid4().hex[:8]}{suffix}"
                try:
                    await download.save_as(str(dest))
                except Exception as e:
                    return {"error": f"Failed to save downloaded file: {e}"}
                rel = f"/api/media/{context.project_id}/{context.conversation_id}/{dest.name}"
                file_size = dest.stat().st_size
                return {
                    "success": True,
                    "filename": suggested,
                    "url": rel,
                    "path": str(dest),
                    "files": [{
                        "id": dest.stem,
                        "url": rel,
                        "name": suggested[:255],
                        "media_type": media_type_for(suggested),
                        "size": file_size,
                    }],
                }

        except Exception as e:
            # If page crashed, clear reference so next call creates a fresh one
            if "Target closed" in str(e) or "crashed" in str(e).lower():
                owner._browser_page = None
            return {"error": str(e)}

        return {"error": f"Unknown operation: {operation}"}
