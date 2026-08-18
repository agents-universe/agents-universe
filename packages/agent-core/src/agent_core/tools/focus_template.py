"""Focus template generator — reads image dimensions and outputs a focus-area JSON template."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolContext


class FocusTemplateTool(Tool):
    name = "focus_template"
    prompt_hint = (
        "First step of screenshot annotation: generates a placeholder JSON template from "
        "an image. Fill in real coordinates and labels, then pass it to image_annotator."
    )
    description = (
        "Read an image file and generate a JSON focus-area template for screenshot annotation. "
        "The template contains placeholder areas that should be adjusted with real coordinates and labels "
        "before passing to image_annotator."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Path to the source image to measure",
            },
            "count": {
                "type": "integer",
                "description": "Number of placeholder focus areas to generate (1-4)",
                "default": 2,
            },
            "units": {
                "type": "string",
                "enum": ["pixel", "percent"],
                "description": "Coordinate unit system for the template",
                "default": "pixel",
            },
            "title": {
                "type": "string",
                "description": "Pre-fill the title field",
                "default": "",
            },
            "subtitle": {
                "type": "string",
                "description": "Pre-fill the subtitle field",
                "default": "",
            },
        },
        "required": ["image_path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        image_path = params["image_path"]
        # LLMs routinely pass integer params as strings — a bare
        # min(max("3", 1), 4) raised TypeError. Clamp like api_request/shell.
        try:
            count = int(params.get("count", 2))
        except (TypeError, ValueError):
            count = 2
        count = min(max(count, 1), 4)
        units = params.get("units", "pixel")
        title = params.get("title", "")
        subtitle = params.get("subtitle", "")

        base = Path(context.project_fs_path).resolve()
        candidate = Path(image_path)
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(base):
            return {"error": f"Access denied: path {image_path!r} is outside project scope"}
        image_path = str(candidate)

        if not Path(image_path).exists():
            return {"error": f"Image not found: {image_path}"}

        try:
            from PIL import Image
        except ImportError:
            return {"error": "Pillow not installed. Run: pip install Pillow"}

        img = Image.open(image_path)
        w, h = img.size
        img.close()

        focus_areas = []
        for i in range(count):
            if units == "percent":
                area = {
                    "xPct": round(10 + i * 25, 1),
                    "yPct": round(30 + i * 15, 1),
                    "widthPct": 20.0,
                    "heightPct": 10.0,
                    "label": f"Focus {i + 1}",
                    "detail": "Replace with why this area matters.",
                }
            else:
                area = {
                    "x": int(w * (0.1 + i * 0.25)),
                    "y": int(h * (0.3 + i * 0.15)),
                    "width": int(w * 0.2),
                    "height": int(h * 0.1),
                    "label": f"Focus {i + 1}",
                    "detail": "Replace with why this area matters.",
                }
            focus_areas.append(area)

        template = {
            "title": title or "Screenshot annotation title",
            "subtitle": subtitle or "Replace with annotation purpose",
            "image_width": w,
            "image_height": h,
            "units": units,
            "focusAreas": focus_areas,
        }

        return {
            "template": template,
            "image_size": {"width": w, "height": h},
            "note": "Replace placeholder labels and coordinates before calling image_annotator.",
        }
