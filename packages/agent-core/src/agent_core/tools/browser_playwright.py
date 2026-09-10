"""Playwright browser tool — headless Chromium automation."""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext
from ._http import _is_ssrf_enabled, validate_outbound_url
from ._media import media_type_for, media_url, sanitize_suffix
from ._ssrf import SSRFError, validate_url
from ._uploads import UploadSourceError, resolve_upload_specs

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


def _recording_filename(requested: Any) -> str:
    """Scenario-named .webm, or a generated one when nobody asked for a name.

    The name travels with the artifact: it is the media record's name, the tail
    of the playback URL, and the filename Jira shows on the attachment. Nothing
    downstream can rename the file (the filesystem tool has no move), so this is
    the only chance to get it right. Characters outside the media router's
    whitelist are collapsed — an unservable name would 400 the playback URL.
    """
    raw = str(requested or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = raw[:-5] if raw.lower().endswith(".webm") else raw
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:80].strip("-")
    if not stem:
        return f"recording_{uuid.uuid4().hex[:8]}.webm"
    return f"{stem}.webm"


def _forget_page(owner: Any, page: Any) -> None:
    """Drop a closed page from the owner's cleanup registry."""
    pages = getattr(owner, "_browser_pages", None)
    if pages and page in pages:
        pages.remove(page)


def _forget_context(owner: Any, context: Any) -> None:
    """Drop a closed context from the owner's cleanup registry."""
    contexts = getattr(owner, "_browser_contexts", None)
    if contexts and context in contexts:
        contexts.remove(context)


async def _register_ssrf_route(page: Any) -> None:
    """Install the SSRF guard on *page* for its whole lifetime.

    It was first registered inside ``goto`` and unrouted in finally — a later
    click (link navigation), form submit, or evaluate("location=...") navigated
    WITHOUT any check, letting an internal/metadata page be read by
    get_text/evaluate afterwards. Registered at page creation, every request of
    every subsequent navigation is validated before it leaves the browser.
    """

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


def _register_context(owner: Any, context: Any) -> None:
    contexts = getattr(owner, "_browser_contexts", None)
    if contexts is None:
        contexts = owner._browser_contexts = []
    contexts.append(context)


def _register_page(owner: Any, page: Any) -> None:
    pages = getattr(owner, "_browser_pages", None)
    if pages is None:
        pages = owner._browser_pages = []
    pages.append(page)


def _remove_tree(path: str) -> None:
    """Best-effort delete of a recording's temp directory."""
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        _log.debug("recording temp dir cleanup failed: %s", path, exc_info=True)


async def _close_context(context: ToolContext, owner: Any, ctx: Any) -> None:
    """Close a context (finalizing its video) and forget it everywhere.

    Closing is what makes Playwright flush the .webm to disk, so this is the
    step that has to happen before a recording can be read back.
    """
    if ctx is None:
        return
    page = getattr(context, "_browser_page", None)
    if page is not None:
        try:
            await page.close()
        except Exception:
            pass
        _forget_page(owner, page)
        context._browser_page = None
    try:
        await ctx.close()
    except Exception:
        _log.debug("browser context close failed", exc_info=True)
    _forget_context(owner, ctx)
    if getattr(context, "_browser_context", None) is ctx:
        context._browser_context = None


async def _get_page(context: ToolContext, owner: Any) -> Any:
    """Return this task's live page, rebuilding context/page when needed.

    Every page the tool drives comes from here, so the SSRF route is installed
    exactly once per page on the only path that can create one. The context is
    explicit (rather than ``browser.new_page``'s implicit one) because
    recording has to swap in a context created with ``record_video_dir`` —
    Playwright cannot add video to a context after the fact.
    """
    browser = await context.ensure_browser()

    page = getattr(context, "_browser_page", None)
    if page is not None:
        try:
            await page.title()
            return page
        except Exception:
            # Closed or crashed: recreate rather than fail every later call.
            try:
                await page.close()
            except Exception:
                pass
            _forget_page(owner, page)
            context._browser_page = None

    ctx = getattr(context, "_browser_context", None)
    if ctx is not None:
        try:
            page = await ctx.new_page()
        except Exception:
            # A closed context cannot host another page; replace it.
            try:
                await ctx.close()
            except Exception:
                pass
            _forget_context(owner, ctx)
            context._browser_context = None
            ctx = None

    if ctx is None:
        ctx = await browser.new_context(
            ignore_https_errors=not getattr(context, "browser_ssl_verify", True),
            accept_downloads=True,
        )
        context._browser_context = ctx
        _register_context(owner, ctx)
        page = None

    if page is None:
        page = await ctx.new_page()
    # Every page this tool can drive funnels through here, so the SSRF route is
    # installed exactly once per page — including a page rebuilt on a context
    # that outlived the old one (an SSRF redirect block closes the page and
    # leaves its context open), which registering only on fresh contexts missed.
    await _register_ssrf_route(page)

    context._browser_page = page
    _register_page(owner, page)
    return page


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
        "fills, file uploads, screenshots, file downloads, screen recording. For plain "
        "text retrieval prefer web_fetch; it is cheaper."
    )
    description = (
        "Control a headless Chromium browser. Supports navigation, clicks, form fills, "
        "selects, checkboxes, hovering, key presses, file uploads (input[type=file] or a "
        "native file chooser), screenshots, file downloads, screen recording, and "
        "JavaScript evaluation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goto", "click", "fill", "select_option", "check", "uncheck", "hover", "press", "upload", "screenshot", "wait_for_selector", "evaluate", "get_text", "bounding_box", "download", "record_start", "record_stop"],
                "description": "Browser operation to perform",
            },
            "url": {"type": "string", "description": "URL for goto operation"},
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector for element operations; for upload, the input[type=file] "
                    "(or, with via_chooser=true, the button that opens the file chooser)"
                ),
            },
            "value": {"type": "string", "description": "Value for fill, or a single option for select_option"},
            "values": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Option values for a multi-select select_option; takes precedence over value",
            },
            "key": {"type": "string", "description": "Key for press, e.g. Enter, Tab, Control+A"},
            "script": {"type": "string", "description": "JavaScript expression for evaluate"},
            "filename": {
                "type": "string",
                "description": (
                    "record_stop only: name the recording after the scenario it proves, "
                    "e.g. 'proj-456-import-orders' (.webm is appended). Use this whenever "
                    "the clip is Jira evidence — the name is what reviewers see."
                ),
            },
            "full_page": {"type": "boolean", "default": True, "description": "Full page screenshot"},
            "timeout": {"type": "integer", "default": 30000, "description": "Timeout in ms"},
            "via_chooser": {
                "type": "boolean",
                "default": False,
                "description": (
                    "upload only: click selector to open a native file chooser and set the "
                    "files on it (for upload buttons with no visible input[type=file])"
                ),
            },
            "files": {
                "type": "array",
                "description": (
                    "Files for upload (max 10, 10MB each). source='attachment' = an in-memory "
                    "chat attachment by stored name; 'path' = project-workspace-relative path "
                    "(hidden files are rejected); 'inline' = content generated here, text in "
                    "content, binary in content_base64."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": ["attachment", "path", "inline"]},
                        "name": {
                            "type": "string",
                            "description": "attachment: stored filename; inline: filename to present; path: optional display override",
                        },
                        "path": {"type": "string", "description": "Workspace-relative path (source=path)"},
                        "content": {"type": "string", "description": "Text content (source=inline, max 1MB)"},
                        "content_base64": {"type": "string", "description": "Base64 content (source=inline binary)"},
                        "mime_type": {"type": "string", "description": "Optional MIME type; inferred from name when omitted"},
                    },
                    "required": ["source"],
                },
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params["operation"]

        # The page is per-TASK state, not per session: it carries the
        # navigation position, so sharing one page between the parallel plan
        # tasks (up to 3) made task A's goto silently replace task B's page and
        # every later get_text/screenshot read the wrong document. The clone
        # keeps its own page; the owner (session context) just records every
        # page so cleanup() can close them all.
        owner = getattr(context, "_shared", None) or context

        # Initialize browser lazily, sharing the session lifecycle with other tools.
        try:
            await context.ensure_browser()
        except Exception as e:
            msg = repr(e) if not str(e) else str(e)
            return {"error": f"Failed to start browser ({type(e).__name__}): {msg}. Run: playwright install chromium"}

        page = await _get_page(context, owner)

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
                    goto_error: str | None = None
                    try:
                        response = await page.goto(url, timeout=timeout)
                        await page.wait_for_load_state("domcontentloaded")
                    except Exception as exc:
                        # An aborted SSRF redirect surfaces here; the post-hoc
                        # check below still decides the outcome.
                        response = None
                        goto_error = str(exc)
                    try:
                        final_url = page.url
                    except Exception:
                        final_url = ""  # page closed/crashed during navigation
                    # Redirects can land on a host the caller's URL check never
                    # saw (e.g. 302 → http://169.254.169.254). Re-run the
                    # literal check against the FINAL URL — only when the
                    # navigation actually landed on an http(s) page: a failed
                    # goto leaves page.url at about:blank, whose scheme check
                    # raised SSRFError and reported every DNS/connection
                    # failure as "blocked by SSRF protection".
                    if final_url.startswith(("http://", "https://")):
                        try:
                            _check_browser_url(final_url)
                        except SSRFError as e:
                            # Close the page so no later operation (get_text,
                            # evaluate, click) can read the internal address.
                            try:
                                await page.close()
                            except Exception:
                                pass
                            context._browser_page = None
                            _forget_page(owner, page)
                            return {"error": f"URL blocked by SSRF protection: {e}"}
                    elif goto_error:
                        result: dict[str, Any] = {"error": f"Navigation failed: {goto_error}"}
                        if failed_requests:
                            result["failed_requests"] = failed_requests
                        return result
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

            elif operation == "select_option":
                selector = params.get("selector", "")
                values = params.get("values")
                if values is None:
                    values = params.get("value")
                if values is None or (isinstance(values, list) and not values):
                    return {"error": "select_option requires 'value' or 'values'"}
                selected = await page.select_option(
                    selector, values, timeout=_clamp_timeout(params.get("timeout", 30000))
                )
                return {"success": True, "selector": selector, "selected": selected}

            elif operation in ("check", "uncheck"):
                selector = params.get("selector", "")
                if not selector:
                    return {"error": f"{operation} requires a 'selector'"}
                method = page.check if operation == "check" else page.uncheck
                await method(selector, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"success": True, "selector": selector, "checked": operation == "check"}

            elif operation == "hover":
                selector = params.get("selector", "")
                if not selector:
                    return {"error": "hover requires a 'selector'"}
                await page.hover(selector, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"success": True, "selector": selector}

            elif operation == "press":
                selector = params.get("selector", "")
                key = params.get("key", "")
                if not selector or not key:
                    return {"error": "press requires 'selector' and 'key'"}
                await page.press(selector, key, timeout=_clamp_timeout(params.get("timeout", 30000)))
                return {"success": True, "selector": selector, "key": key}

            elif operation == "upload":
                specs = params.get("files")
                if specs is None:
                    return {"error": "upload requires 'files'"}
                selector = params.get("selector", "")
                if not selector:
                    return {
                        "error": "upload requires 'selector' (an input[type=file], or the triggering button with via_chooser=true)"
                    }
                via_chooser = bool(params.get("via_chooser", False))
                timeout = _clamp_timeout(params.get("timeout", 30000))
                try:
                    payloads = resolve_upload_specs(context, specs)
                except UploadSourceError as e:
                    return {"error": str(e)}
                # In-memory FilePayloads: Playwright passes them straight to the
                # renderer, so a chat attachment never has to be written to disk
                # to be uploaded.
                buffers = [
                    {"name": p.name, "mimeType": p.mime_type, "buffer": p.data} for p in payloads
                ]
                if via_chooser:
                    async with page.expect_file_chooser(timeout=timeout) as chooser_info:
                        await page.click(selector, timeout=timeout)
                    chooser = await chooser_info.value
                    await chooser.set_files(buffers)
                else:
                    await page.set_input_files(selector, buffers, timeout=timeout)
                # Deliberately NOT result["files"]: the agent loop treats any
                # file entry carrying a url as a user-facing deliverable and
                # emits file_output for it (agent.py), which would surface the
                # uploaded payload as if the agent had produced it.
                return {
                    "success": True,
                    "selector": selector,
                    "via_chooser": via_chooser,
                    "uploaded": [
                        {"name": p.name, "size": len(p.data), "mime_type": p.mime_type, "source": p.source}
                        for p in payloads
                    ],
                }

            elif operation == "bounding_box":
                # Measure an element's position in SCREENSHOT coordinates so
                # image_annotator boxes land exactly on it. This exists because
                # getBoundingClientRect() is viewport-relative while screenshots
                # default to full_page=true (document coordinates) - annotating
                # from raw rects without the scrollY conversion always misses.
                selector = params.get("selector", "")
                if not selector:
                    return {"error": "bounding_box requires a 'selector'"}
                try:
                    await page.wait_for_selector(selector, timeout=_clamp_timeout(params.get("timeout", 30000)))
                except Exception as e:
                    return {"error": f"Selector not found: {selector} ({e})"}
                box = await page.evaluate(
                    """
                    (sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        const dpr = window.devicePixelRatio || 1;
                        return {
                            viewport: {
                                x: Math.round(r.x), y: Math.round(r.y),
                                width: Math.round(r.width), height: Math.round(r.height),
                            },
                            fullPage: {
                                x: Math.round(r.x),
                                y: Math.round(r.y + window.scrollY),
                                width: Math.round(r.width),
                                height: Math.round(r.height),
                            },
                            scrollX: window.scrollX, scrollY: window.scrollY,
                            documentHeight: document.documentElement.scrollHeight,
                            viewportHeight: window.innerHeight,
                            devicePixelRatio: dpr,
                        };
                    }
                    """,
                    selector,
                )
                if box is None:
                    return {"error": f"Element not found: {selector}"}
                note = (
                    "Use 'viewport' for full_page=false screenshots, 'fullPage' for "
                    "full_page=true screenshots. Playwright screenshots are CSS-pixel "
                    "accurate regardless of devicePixelRatio."
                )
                return {"selector": selector, "bounding_box": box, "note": note}

            elif operation == "screenshot":
                media_path = Path(context.conversation_media_dir)
                media_path.mkdir(parents=True, exist_ok=True)
                filename = f"screenshot_{uuid.uuid4().hex[:8]}.png"
                screenshot_path = str(media_path / filename)
                full_page = params.get("full_page", True)
                await page.screenshot(path=screenshot_path, full_page=full_page)
                rel_path = media_url(context, filename)
                return {
                    "screenshot_path": screenshot_path,
                    "url": rel_path,
                    "title": await page.title(),
                    "page_url": page.url,
                    "images": [{"id": filename, "url": rel_path, "alt": f"Screenshot of {page.url}", "path": screenshot_path}],
                }

            elif operation == "record_start":
                if getattr(context, "_browser_recording", None) is not None:
                    return {"error": "Already recording; call record_stop first"}
                # Playwright cannot add video to a context after creation, so
                # recording means a NEW context. Carry the session over with
                # storage_state (cookies + localStorage) and re-open the same
                # URL, so the recording starts where the session already was.
                state = None
                resume_url = ""
                old_ctx = getattr(context, "_browser_context", None)
                if old_ctx is not None:
                    try:
                        resume_url = page.url if page.url.startswith(("http://", "https://")) else ""
                    except Exception:
                        resume_url = ""
                    try:
                        state = await old_ctx.storage_state()
                    except Exception:
                        state = None
                await _close_context(context, owner, old_ctx)
                page = None

                tmp_dir = Path(context.project_fs_path) / ".tmp" / "work" / "recordings" / context.conversation_id / f"recording_{uuid.uuid4().hex[:8]}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                browser = await context.ensure_browser()
                ctx_kwargs: dict[str, Any] = {
                    "ignore_https_errors": not getattr(context, "browser_ssl_verify", True),
                    "accept_downloads": True,
                    "record_video_dir": str(tmp_dir),
                    "record_video_size": {"width": 1280, "height": 720},
                }
                if state:
                    ctx_kwargs["storage_state"] = state
                new_ctx = await browser.new_context(**ctx_kwargs)
                context._browser_context = new_ctx
                _register_context(owner, new_ctx)
                page = await new_ctx.new_page()
                await _register_ssrf_route(page)
                context._browser_page = page
                _register_page(owner, page)

                result = {
                    "recording": True,
                    "note": (
                        "Recording started in a new browser context. Call record_stop to "
                        "finalize and save the .webm — a recording that is never stopped is "
                        "discarded when the turn ends. The new context kept cookies and "
                        "localStorage but not in-page state, so re-run any steps that "
                        "depended on a half-filled form."
                    ),
                }
                if resume_url:
                    try:
                        _check_browser_url(resume_url)
                        await page.goto(resume_url, timeout=_clamp_timeout(params.get("timeout", 30000)))
                        await page.wait_for_load_state("domcontentloaded")
                        result["page_url"] = resume_url
                    except Exception as e:
                        result["navigation_error"] = f"{resume_url} could not be reopened: {e}"
                context._browser_recording = {
                    "context": new_ctx,
                    "page": page,
                    "video": page.video,
                    "dir": str(tmp_dir),
                    "started": time.monotonic(),
                }
                return result

            elif operation == "record_stop":
                rec = getattr(context, "_browser_recording", None)
                if rec is None:
                    return {"error": "No active recording. Call record_start first."}
                context._browser_recording = None
                # Closing the context is what writes the .webm — until then the
                # buffer is only in the browser process.
                await _close_context(context, owner, rec["context"])
                page = None

                media_path = Path(context.conversation_media_dir)
                media_path.mkdir(parents=True, exist_ok=True)
                filename = _recording_filename(params.get("filename"))
                dest = media_path / filename
                try:
                    await rec["video"].save_as(str(dest))
                except Exception as e:
                    _remove_tree(rec["dir"])
                    return {"error": f"Failed to save recording: {e}"}
                try:
                    await rec["video"].delete()
                except Exception:
                    pass
                _remove_tree(rec["dir"])

                size = dest.stat().st_size if dest.exists() else 0
                rel = media_url(context, filename)
                result = {
                    "success": True,
                    "filename": filename,
                    "url": rel,
                    "path": str(dest),
                    "size": size,
                    "duration_ms": int((time.monotonic() - rec["started"]) * 1000),
                    "files": [{
                        "id": dest.stem,
                        "url": rel,
                        "name": filename,
                        "media_type": "video/webm",
                        "size": size,
                    }],
                }
                if size < 1024:
                    result["warning"] = (
                        "Recording is only a few bytes — the .webm is unlikely to be usable "
                        "evidence. Record the scenario again with a short pause between "
                        "record_start and record_stop."
                    )
                return result

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
                rel = media_url(context, dest.name)
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
                context._browser_page = None
                _forget_page(owner, page)
                ctx = getattr(context, "_browser_context", None)
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                    context._browser_context = None
                    _forget_context(owner, ctx)
            return {"error": str(e)}

        return {"error": f"Unknown operation: {operation}"}
