"""Image annotator tool — adds boxes, arrows, and labels to screenshots using Pillow."""
from __future__ import annotations

import logging
import math
import uuid
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# pixel dimension cap checked BEFORE decoding (decompression-bomb
# guard) — RGBA is 4 bytes/pixel, so a 20000x20000 PNG would allocate ~1.6GB.
_MAX_IMAGE_DIMENSION = 8192

# CJK-aware font chain: Linux containers ship fonts-noto-cjk (Noto Sans CJK),
# Windows dev hosts provide arial.ttf. Pillow's default bitmap font carries no
# CJK glyphs, so Chinese labels would render as boxes with it.
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "arial.ttf",
)

# Supersampling: shapes (rounded outlines, pill chips) are drawn on a 2x
# transparent overlay and downscaled with LANCZOS, which anti-aliases edges that
# Pillow would otherwise render hard/aliased. Text stays at 1x for crispness.
# Disabled above this pixel budget so a 4K full-page capture doesn't allocate a
# half-gigabyte overlay.
_SS_PIXEL_BUDGET = 4_000_000

# Default color palette rotation for focus areas (QA evidence often marks
# several distinct areas — a single color makes them hard to tell apart).
_DEFAULT_PALETTE = (
    "#FF6B00",  # orange (kept first for backwards compatibility)
    "#2563EB",  # blue
    "#16A34A",  # green
    "#DC2626",  # red
    "#9333EA",  # purple
    "#0D9488",  # teal
)

# Neutral UI grays for header/footer chrome.
_COLOR_TITLE = (31, 41, 55, 255)      # gray-800
_COLOR_SUBTITLE = (107, 114, 128, 255)  # gray-500
_COLOR_DETAIL = (75, 85, 99, 255)     # gray-600
_COLOR_DIVIDER = (229, 231, 235, 255)  # gray-200


def _load_font(size: int) -> Any:
    """Return the first loadable candidate font, else Pillow's bitmap default."""
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default(size)


def _text_width(draw: Any, text: str, font: Any) -> int:
    """Return the rendered text width, falling back to a heuristic on old Pillow."""
    try:
        return int(draw.textlength(text, font=font))
    except (AttributeError, TypeError):
        # CJK glyphs are roughly as wide as the font size, Latin roughly half.
        cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
        return cjk * font.size + (len(text) - cjk) * font.size // 2


class ImageAnnotatorTool(Tool):
    name = "image_annotator"
    prompt_hint = (
        "Draw highlight boxes and labels on an existing image (e.g. to point out areas "
        "of a screenshot). Pair with focus_template to draft the annotation JSON first."
    )
    description = (
        "Annotate an existing image by drawing highlight boxes, labels, and focus areas. "
        "Supports both raw annotation mode and focus-template mode (JSON with title/subtitle/focusAreas). "
        "Boxes are drawn as hollow rounded outlines with a white halo so the annotated "
        "content stays visible; multiple areas rotate through a color palette. "
        "Returns the path to the annotated image."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Absolute path to the source image",
            },
            "output_path": {
                "type": "string",
                "description": "Optional output path. Defaults to {stem}-annotated.png in same directory.",
            },
            "title": {
                "type": "string",
                "description": "Optional title shown at the top of the annotated image",
            },
            "subtitle": {
                "type": "string",
                "description": "Optional subtitle shown below the title",
            },
            "focus_areas": {
                "type": "array",
                "description": "Focus area annotations (preferred mode for Jira evidence)",
                "items": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "X pixel coordinate"},
                        "y": {"type": "number", "description": "Y pixel coordinate"},
                        "width": {"type": "number", "description": "Width in pixels"},
                        "height": {"type": "number", "description": "Height in pixels"},
                        "xPct": {"type": "number", "description": "X as percentage (0-100)"},
                        "yPct": {"type": "number", "description": "Y as percentage (0-100)"},
                        "widthPct": {"type": "number", "description": "Width as percentage (0-100)"},
                        "heightPct": {"type": "number", "description": "Height as percentage (0-100)"},
                        "label": {"type": "string", "description": "Short label for the area"},
                        "detail": {"type": "string", "description": "Why this area matters"},
                        "color": {"type": "string", "description": "Hex color (default: palette rotation starting #FF6B00)"},
                    },
                    "required": ["label"],
                },
            },
            "annotations": {
                "type": "array",
                "description": "Raw annotation objects (legacy mode)",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["box", "circle", "label", "arrow", "highlight"]},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "w": {"type": "number"},
                        "h": {"type": "number"},
                        "label": {"type": "string"},
                        "color": {"type": "string", "default": "#FF6B00"},
                    },
                    "required": ["type", "x", "y"],
                },
            },
        },
        "required": ["image_path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        project_root = Path(context.project_fs_path).resolve()

        # Resolve the source image and enforce it stays inside the project workspace
        image_path = params["image_path"]
        raw_path = Path(image_path)
        if not raw_path.is_absolute():
            raw_path = project_root / raw_path
        try:
            resolved = raw_path.resolve()
            resolved.relative_to(project_root)
        except (OSError, ValueError):
            return {"error": f"Image path must be inside the project workspace: {image_path}"}
        image_path = str(resolved)

        if not Path(image_path).exists():
            return {"error": f"Image not found: {image_path}"}

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            _log.warning("image_annotator: Pillow not installed")
            return {"error": "Pillow not installed. Run: pip install Pillow"}

        try:
            # Image.open is lazy (header only), but .convert("RGBA")
            # decodes every pixel — a small compressed PNG can be
            # 20000x20000 and allocate ~1.6GB RGBA. Check dimensions BEFORE
            # decoding (PIL reads the header without touching pixel data).
            with Image.open(image_path) as probe:
                p_w, p_h = probe.size
            if p_w > _MAX_IMAGE_DIMENSION or p_h > _MAX_IMAGE_DIMENSION:
                return {"error": (
                    f"Image too large: {p_w}x{p_h} (max dimension "
                    f"{_MAX_IMAGE_DIMENSION}) — downscale it first"
                )}
            img = Image.open(image_path).convert("RGBA")
        except Exception as e:
            _log.warning("image_annotator: failed to open/convert %s: %s", image_path, e)
            return {"error": f"Failed to open image: {e}"}
        img_w, img_h = img.size

        title = params.get("title", "")
        subtitle = params.get("subtitle", "")
        focus_areas = params.get("focus_areas", []) or []
        annotations = params.get("annotations", []) or []

        # ---- typography scales with image size: a fixed 11px chip on a 4K ----
        # capture is unreadable; a scaled 22px chip on a 640px thumbnail is huge.
        scale = max(1.0, min(2.0, img_w / 1280))
        font_small = _load_font(max(11, round(11 * scale)))
        font_title = _load_font(max(15, round(16 * scale)))

        outline_width = max(3, min(6, round(2.5 * scale)))
        chip_h = font_small.size + 10
        chip_pad = max(6, round(6 * scale))

        # ---- header / footer geometry ----
        header_height = (30 if title else 0) + (20 if subtitle else 0)

        footer_lines = [fa.get("detail", "") for fa in focus_areas if fa.get("detail")]
        footer_line_h = font_small.size + 7
        footer_height = (8 + len(footer_lines) * footer_line_h + 6) if footer_lines else 0

        canvas_h = img_h + header_height + footer_height
        canvas = Image.new("RGBA", (img_w, canvas_h), (255, 255, 255, 255))
        canvas.paste(img, (0, header_height))
        # Release the source file handle (focus_template.py does the same).
        # Without this, Windows keeps the file locked until GC runs.
        img.close()

        # ---- supersampled shape overlay (hollow boxes, pills, arrows) ----
        ss = 2 if (img_w * canvas_h) <= _SS_PIXEL_BUDGET else 1
        overlay = Image.new("RGBA", (img_w * ss, canvas_h * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        def S(v: float) -> int:
            """Scale a 1x coordinate onto the supersampled overlay."""
            return int(round(v * ss))

        def parse_color(color_str: str) -> tuple:
            try:
                r = int(color_str[1:3], 16)
                g = int(color_str[3:5], 16)
                b = int(color_str[5:7], 16)
                return (r, g, b)
            except Exception:
                return (255, 107, 0)

        def clamp_box(x: int, y: int, w: int, h: int) -> tuple:
            """Degenerate/negative boxes would make rounded_rectangle misbehave."""
            return x, y, max(w, 2), max(h, 2)

        def draw_hollow_box(x: int, y: int, w: int, h: int, color_solid: tuple) -> None:
            """Hollow rounded outline with a white halo ring. The halo is drawn
            strictly OUTSIDE the box so every interior pixel except the colored
            stroke itself stays identical to the source; it makes the outline
            readable on both light and dark backgrounds."""
            x, y, w, h = clamp_box(x, y, w, h)
            r = max(4, min(18, min(w, h) // 6))
            r = min(r, w // 2, h // 2)
            halo = 3
            d.rounded_rectangle(
                [S(x - halo), S(y - halo), S(x + w + halo), S(y + h + halo)],
                radius=S(min(r + halo, (w + 2 * halo) // 2, (h + 2 * halo) // 2)),
                outline=(255, 255, 255, 255), width=S(halo),
            )
            d.rounded_rectangle(
                [S(x), S(y), S(x + w), S(y + h)],
                radius=S(r), outline=color_solid, width=S(outline_width),
            )

        def chip_geometry(
            d_text: Any,
            box_x: int,
            box_y: int,
            box_w: int,
            box_h: int,
            text: str,
        ) -> tuple:
            """Compute the pill chip rect for a label. Default sits above the
            box's top-left corner; flips below when the box hugs the header;
            clamped horizontally so it never leaves the image area."""
            cw = _text_width(d_text, text, font_small) + chip_pad * 2
            cy = box_y - chip_h - 4
            if cy < header_height:
                cy = box_y + box_h + 4
            cy = min(cy, header_height + img_h - chip_h - 2)
            cx = min(max(box_x, 2), img_w - cw - 2)
            return cx, cy, cw

        def draw_chip(cx: int, cy: int, cw: int, color_solid: tuple) -> None:
            """Pill-shaped label background with a thin white rim."""
            r = chip_h // 2
            d.rounded_rectangle(
                [S(cx), S(cy), S(cx + cw), S(cy + chip_h)],
                radius=S(r), fill=color_solid,
                outline=(255, 255, 255, 255), width=S(2),
            )

        # chip texts are drawn AFTER the overlay is downscaled + composited,
        # so freetype renders them at native 1x (crisper than downscaling).
        chip_texts: list[tuple[int, int, str]] = []

        # ---- focus areas mode — hollow outlines only: never fill the box, so ----
        # the annotated UI/content underneath stays fully readable.
        for i, fa in enumerate(focus_areas):
            color_rgb = parse_color(fa.get("color") or _DEFAULT_PALETTE[i % len(_DEFAULT_PALETTE)])
            color_solid = (*color_rgb, 255)

            if all(k in fa for k in ("xPct", "yPct", "widthPct", "heightPct")):
                x = int(fa["xPct"] / 100 * img_w)
                y = int(fa["yPct"] / 100 * img_h) + header_height
                w = int(fa["widthPct"] / 100 * img_w)
                h = int(fa["heightPct"] / 100 * img_h)
            else:
                x = int(fa.get("x", 0))
                y = int(fa.get("y", 0)) + header_height
                w = int(fa.get("width", 100))
                h = int(fa.get("height", 50))

            draw_hollow_box(x, y, w, h, color_solid)

            label = fa.get("label", f"Focus {i + 1}")
            chip_label = f"{i + 1}. {label}" if footer_lines else label
            cx, cy, cw = chip_geometry(d, x, y, w, h, chip_label)
            draw_chip(cx, cy, cw, color_solid)
            chip_texts.append((cx + chip_pad, cy + (chip_h - font_small.size) // 2 - 1, chip_label))

        # ---- legacy annotations mode ----
        for ann in annotations:
            ann_type = ann.get("type", "box")
            color_rgb = parse_color(ann.get("color", "#FF6B00"))
            color_solid = (*color_rgb, 255)

            x = int(ann.get("x", 0))
            y = int(ann.get("y", 0)) + header_height
            w = int(ann.get("w", 100))
            h = int(ann.get("h", 50))
            label = ann.get("label", "")

            if ann_type in ("box", "highlight"):
                # Hollow highlight: keep the underlying content readable.
                draw_hollow_box(x, y, w, h, color_solid)
                if label:
                    cx, cy, cw = chip_geometry(d, x, y, w, h, label)
                    draw_chip(cx, cy, cw, color_solid)
                    chip_texts.append((cx + chip_pad, cy + (chip_h - font_small.size) // 2 - 1, label))
            elif ann_type == "circle":
                d.ellipse(
                    [S(x - w // 2), S(y - h // 2), S(x + w // 2), S(y + h // 2)],
                    outline=color_solid, width=S(outline_width),
                )
                if label:
                    cx, cy, cw = chip_geometry(d, x - w // 2, y - h // 2, w, h, label)
                    draw_chip(cx, cy, cw, color_solid)
                    chip_texts.append((cx + chip_pad, cy + (chip_h - font_small.size) // 2 - 1, label))
            elif ann_type == "label":
                cw = _text_width(d, label, font_small) + chip_pad * 2
                cy = max(y - chip_h - 2, header_height)
                cx = min(max(x, 2), img_w - cw - 2)
                draw_chip(cx, cy, cw, color_solid)
                chip_texts.append((cx + chip_pad, cy + (chip_h - font_small.size) // 2 - 1, label))
            elif ann_type == "arrow":
                x2 = int(ann.get("x2", x + 50))
                y2 = int(ann.get("y2", y + 50))
                if "y2" in ann:
                    y2 += header_height
                d.line([S(x), S(y), S(x2), S(y2)], fill=color_solid, width=S(outline_width))
                # arrowhead: triangle with tip at (x2, y2), base corners at
                # +/-155 degrees from the line direction
                head = max(8, outline_width * 3)
                ang = math.atan2(y2 - y, x2 - x)
                base = [
                    (S(x2 + head * math.cos(ang + math.radians(155))),
                     S(y2 + head * math.sin(ang + math.radians(155)))),
                    (S(x2 + head * math.cos(ang + math.radians(-155))),
                     S(y2 + head * math.sin(ang + math.radians(-155)))),
                ]
                d.polygon([(S(x2), S(y2)), *base], fill=color_solid)

        # ---- downscale overlay, composite, then draw all text at native 1x ----
        if ss > 1:
            overlay = overlay.resize((img_w, canvas_h), Image.LANCZOS)
        result_img = Image.alpha_composite(canvas, overlay).convert("RGB")
        draw = ImageDraw.Draw(result_img)

        for tx, ty, text in chip_texts:
            draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font_small)

        if title:
            draw.text((10, 6), title, fill=_COLOR_TITLE, font=font_title)
            draw.line([(0, header_height - 1), (img_w, header_height - 1)],
                      fill=_COLOR_DIVIDER, width=1)
        if subtitle:
            draw.text((10, 28 if title else 6), subtitle, fill=_COLOR_SUBTITLE, font=font_small)

        if footer_lines:
            div_y = img_h + header_height
            draw.line([(0, div_y + 1), (img_w, div_y + 1)], fill=_COLOR_DIVIDER, width=1)
            y_offset = div_y + 8
            for i, detail in enumerate(footer_lines):
                marker = f"[{i + 1}] "
                draw.text((10, y_offset), marker + detail, fill=_COLOR_DETAIL, font=font_small)
                y_offset += footer_line_h

        # Determine output path — default to the conversation media dir so the
        # returned /api/media URL actually resolves. Any explicit output path
        # must stay inside the project workspace.
        media_dir = Path(context.conversation_media_dir)
        url: str | None = None
        output_path = params.get("output_path")
        if not output_path:
            media_dir.mkdir(parents=True, exist_ok=True)
            # Server-generated name: the source stem (often Chinese or
            # space-containing) violates the media URL whitelist and would
            # render a 404 broken image. Mirror code_executor: uuid + clean
            # suffix, source name rides in the record. The output is always
            # saved as PNG, so the extension is fixed too.
            fname = f"annotated_{uuid.uuid4().hex[:8]}.png"
            output_path = str(media_dir / fname)
            url = f"/api/media/{context.project_id}/{context.conversation_id}/{fname}"
        else:
            out = Path(output_path)
            if not out.is_absolute():
                out = project_root / out
            try:
                out = out.resolve()
                out.relative_to(project_root)
            except (OSError, ValueError):
                return {"error": f"Output path must be inside the project workspace: {output_path}"}
            output_path = str(out)

        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            result_img.save(output_path, "PNG")
        except Exception as e:
            _log.warning("image_annotator: failed to save %s: %s", output_path, e)
            return {"error": f"Failed to save annotated image: {e}"}

        if url is None:
            # Explicit output path outside the conversation media dir is not
            # servable via /api/media — returning a dead URL would render a
            # broken image in the UI.
            return {
                "annotated_path": output_path,
                "width": img_w,
                "height": canvas_h,
                "focus_areas_count": len(focus_areas),
                "annotations_count": len(annotations),
            }

        return {
            "annotated_path": output_path,
            "url": url,
            "width": img_w,
            "height": canvas_h,
            "focus_areas_count": len(focus_areas),
            "annotations_count": len(annotations),
            "note": (
                "Boxes render at the exact pixel coordinates provided (plus the "
                f"{header_height}px header offset). If a box looks misplaced, the input "
                "coordinates were wrong - measure them with browser_playwright "
                "bounding_box (viewport coords for viewport screenshots, y+scrollY for "
                "full-page) instead of guessing."
            ),
            "images": [{"id": "annotated", "url": url, "alt": title or "Annotated screenshot", "path": output_path}],
        }
