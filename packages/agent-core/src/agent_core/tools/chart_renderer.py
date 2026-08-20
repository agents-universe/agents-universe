"""Server-side Mermaid renderer backed by the session Playwright browser."""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

_MAX_CODE_LENGTH = 100_000
_MIN_WIDTH = 200
_MAX_WIDTH = 4_000
_ALLOWED_THEMES = frozenset({"default", "neutral", "dark", "forest", "base"})


def _find_mermaid_bundle() -> Path | None:
    """Find the local Mermaid distribution without ever falling back to a CDN."""
    candidates: list[Path] = []
    configured = os.environ.get("MERMAID_BUNDLE_PATH")
    if configured:
        candidates.append(Path(configured))

    candidates.append(Path("/app/vendor/mermaid/mermaid.min.js"))

    # Local development path: walk up from this file and look for the monorepo
    # web package.  Installed/container paths are shorter than the source tree,
    # so avoid fixed parents[n] indexing (IndexError stringifies as "5").
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / "packages" / "web" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js")

    return next((path for path in candidates if path.is_file()), None)


def _error(code: str, message: str) -> dict[str, Any]:
    """Return a safe, structured tool error without local path details."""
    return {"error": {"code": code, "message": message}}


def _safe_error_message(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    # Playwright errors can include local filenames; do not reveal host paths.
    message = re.sub(r"(?:[A-Za-z]:)?[/\\][^\s:]+", "[local path]", message)
    return message[:1_000] or type(exc).__name__


class ChartRendererTool(Tool):
    """Render Mermaid source in a disposable page and return its PNG artifact."""

    name = "chart_renderer"
    prompt_hint = (
        "Run every Mermaid diagram through this before showing it to the user — it "
        "validates the source and only returns an image when rendering succeeds. "
        "The rendered PNG is displayed to the user automatically; do not repeat "
        "the Mermaid source or embed image markdown in your reply."
    )
    description = (
        "Render and validate Mermaid diagram source before presenting it to the user. "
        "Call this whenever you generate Mermaid code; it returns a PNG only when Mermaid "
        "successfully parsed and rendered an SVG."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Mermaid diagram source (maximum 100000 characters)."},
            "theme": {
                "type": "string",
                "enum": sorted(_ALLOWED_THEMES),
                "default": "dark",
                "description": "Mermaid theme.",
            },
            "width": {
                "type": "integer",
                "minimum": _MIN_WIDTH,
                "maximum": _MAX_WIDTH,
                "default": 1200,
                "description": "Viewport and diagram width in pixels.",
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            return _error("invalid_code", "Mermaid code must be a non-empty string.")
        if len(code) > _MAX_CODE_LENGTH:
            return _error("code_too_large", f"Mermaid code must not exceed {_MAX_CODE_LENGTH} characters.")

        theme = params.get("theme", "dark")
        if theme not in _ALLOWED_THEMES:
            return _error("invalid_theme", "Unsupported Mermaid theme.")
        width = params.get("width", 1200)
        if isinstance(width, bool) or not isinstance(width, int) or not _MIN_WIDTH <= width <= _MAX_WIDTH:
            return _error("invalid_width", f"Width must be an integer between {_MIN_WIDTH} and {_MAX_WIDTH}.")

        bundle = _find_mermaid_bundle()
        if bundle is None:
            return _error("mermaid_bundle_missing", "Local Mermaid bundle is unavailable; contact an administrator.")
        page = None
        try:
            browser = await context.ensure_browser()
            page = await browser.new_page(viewport={"width": width, "height": 900})
            await page.set_content(
                "<!doctype html><html><body style='margin:0;padding:16px;background:#1e1e1e'>"
                f"<div id='chart' style='width:{width}px'></div></body></html>"
            )
            await page.add_script_tag(path=str(bundle))
            rendered = await page.evaluate(
                """async ({ code, theme }) => {
                    mermaid.initialize({ startOnLoad: false, theme, securityLevel: 'strict' });
                    await mermaid.parse(code);
                    const { svg } = await mermaid.render('mermaid-chart', code);
                    const container = document.getElementById('chart');
                    container.innerHTML = svg;
                    const svgElement = container.querySelector('svg');
                    if (!svgElement) throw new Error('Mermaid did not produce an SVG.');
                    return { width: Math.ceil(svgElement.getBoundingClientRect().width),
                             height: Math.ceil(svgElement.getBoundingClientRect().height) };
                }""",
                {"code": code, "theme": theme},
            )
            if not isinstance(rendered, dict) or rendered.get("width", 0) <= 0 or rendered.get("height", 0) <= 0:
                return _error("render_failed", "Mermaid did not produce a visible SVG.")

            media_dir = Path(context.conversation_media_dir)
            media_dir.mkdir(parents=True, exist_ok=True)
            filename = f"mermaid_{uuid.uuid4().hex[:8]}.png"
            output_path = media_dir / filename
            await page.screenshot(path=str(output_path), full_page=True)
            url = f"/api/media/{context.project_id}/{context.conversation_id}/{filename}"
            return {
                "success": True,
                "width": rendered["width"],
                "height": rendered["height"],
                "url": url,
                "images": [{
                    "id": filename,
                    "url": url,
                    "alt": "Rendered Mermaid diagram",
                    "path": str(output_path),
                    "annotations": [],
                }],
            }
        except Exception as exc:
            msg = _safe_error_message(exc)
            _log.warning("chart_renderer failed: %s (code length=%d, theme=%s)", msg, len(code), theme)
            return _error("render_failed", f"Mermaid rendering failed: {msg}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
