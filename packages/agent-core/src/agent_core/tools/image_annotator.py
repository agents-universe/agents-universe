"""Image annotator tool — adds boxes, arrows, and labels to screenshots using Pillow."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# pixel dimension cap checked BEFORE decoding (decompression-bomb
# guard) — RGBA is 4 bytes/pixel, so a 20000x20000 PNG would allocate ~1.6GB.
_MAX_IMAGE_DIMENSION = 8192


class ImageAnnotatorTool(Tool):
    name = "image_annotator"
    prompt_hint = (
        "Draw highlight boxes and labels on an existing image (e.g. to point out areas "
        "of a screenshot). Pair with focus_template to draft the annotation JSON first."
    )
    description = (
        "Annotate an existing image by drawing highlight boxes, labels, and focus areas. "
        "Supports both raw annotation mode and focus-template mode (JSON with title/subtitle/focusAreas). "
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
                        "color": {"type": "string", "description": "Hex color (default: #FF6B00)"},
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
        focus_areas = params.get("focus_areas", [])
        annotations = params.get("annotations", [])

        header_height = 0
        if title:
            header_height += 30
        if subtitle:
            header_height += 20

        footer_lines = [fa.get("detail", "") for fa in focus_areas if fa.get("detail")]
        footer_height = len(footer_lines) * 18 + (10 if footer_lines else 0)

        canvas_h = img_h + header_height + footer_height
        canvas = Image.new("RGBA", (img_w, canvas_h), (255, 255, 255, 255))
        canvas.paste(img, (0, header_height))
        # Release the source file handle (focus_template.py does the same).
        # Without this, Windows keeps the file locked until GC runs.
        img.close()

        draw = ImageDraw.Draw(canvas)

        try:
            font = ImageFont.truetype("arial.ttf", 14)
            font_small = ImageFont.truetype("arial.ttf", 11)
            font_title = ImageFont.truetype("arial.ttf", 16)
        except (OSError, IOError):
            font = ImageFont.load_default()
            font_small = font
            font_title = font

        if title:
            draw.text((10, 6), title, fill=(30, 30, 30, 255), font=font_title)
        if subtitle:
            draw.text((10, 28 if title else 6), subtitle, fill=(100, 100, 100, 255), font=font_small)

        def parse_color(color_str: str) -> tuple:
            try:
                r = int(color_str[1:3], 16)
                g = int(color_str[3:5], 16)
                b = int(color_str[5:7], 16)
                return (r, g, b)
            except Exception:
                return (255, 107, 0)

        # Focus areas mode
        for i, fa in enumerate(focus_areas):
            color_rgb = parse_color(fa.get("color", "#FF6B00"))
            color_solid = (*color_rgb, 255)
            color_fill = (*color_rgb, 40)

            if "xPct" in fa and "yPct" in fa and "widthPct" in fa and "heightPct" in fa:
                x = int(fa["xPct"] / 100 * img_w)
                y = int(fa["yPct"] / 100 * img_h) + header_height
                w = int(fa["widthPct"] / 100 * img_w)
                h = int(fa["heightPct"] / 100 * img_h)
            else:
                x = int(fa.get("x", 0))
                y = int(fa.get("y", 0)) + header_height
                w = int(fa.get("width", 100))
                h = int(fa.get("height", 50))

            draw.rectangle([x, y, x + w, y + h], fill=color_fill, outline=color_solid, width=3)

            label = fa.get("label", f"Focus {i + 1}")
            label_w = len(label) * 8 + 12
            draw.rectangle([x, y - 20, x + label_w, y], fill=color_solid)
            draw.text((x + 6, y - 17), label, fill=(255, 255, 255, 255), font=font_small)

        # Footer details
        if footer_lines:
            y_offset = img_h + header_height + 6
            for i, detail in enumerate(footer_lines):
                marker = f"[{i + 1}] "
                draw.text((10, y_offset), marker + detail, fill=(60, 60, 60, 255), font=font_small)
                y_offset += 18

        # Legacy annotations mode
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)

        for ann in annotations:
            ann_type = ann.get("type", "box")
            color_rgb = parse_color(ann.get("color", "#FF6B00"))
            color = (*color_rgb, 180)
            color_solid = (*color_rgb, 255)

            x = int(ann.get("x", 0))
            y = int(ann.get("y", 0)) + header_height
            w = int(ann.get("w", 100))
            h = int(ann.get("h", 50))
            label = ann.get("label", "")

            if ann_type == "box":
                draw_overlay.rectangle([x, y, x + w, y + h], outline=color_solid, width=2)
                if label:
                    draw_overlay.rectangle([x, y - 18, x + len(label) * 7 + 8, y], fill=color)
                    draw_overlay.text((x + 4, y - 16), label, fill=(255, 255, 255, 255))
            elif ann_type == "highlight":
                draw_overlay.rectangle([x, y, x + w, y + h], fill=(*color_rgb, 60), outline=color_solid, width=2)
            elif ann_type == "circle":
                draw_overlay.ellipse([x - w // 2, y - h // 2, x + w // 2, y + h // 2], outline=color_solid, width=2)
            elif ann_type == "label":
                draw_overlay.rectangle([x, y - 18, x + len(label) * 7 + 8, y], fill=color)
                draw_overlay.text((x + 4, y - 16), label, fill=(255, 255, 255, 255))
            elif ann_type == "arrow":
                x2 = int(ann.get("x2", x + 50))
                y2 = int(ann.get("y2", y + 50))
                if "y2" in ann:
                    y2 += header_height
                draw_overlay.line([x, y, x2, y2], fill=color_solid, width=3)

        result_img = Image.alpha_composite(canvas, overlay).convert("RGB")

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
            "images": [{"id": "annotated", "url": url, "alt": title or "Annotated screenshot", "path": output_path}],
        }
