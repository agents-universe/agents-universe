"""deliver_file tool — hand a project workspace file to the user.

Copies the file into the conversation's media directory, which /api/media
serves with per-user auth (only the conversation owner can download). Files
already inside the media directory are returned as-is without a copy.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from ._media import media_type_for, sanitize_suffix
from .base import Tool, ToolContext


class DeliverFileTool(Tool):
    name = "deliver_file"
    prompt_hint = (
        "Use to hand the user a file from the workspace (shows up as a downloadable "
        "attachment in chat). Files written to the code_executor OUTPUT_DIR are "
        "delivered automatically; this tool covers everything else."
    )
    description = (
        "Deliver a file from the project workspace to the user as a downloadable "
        "attachment in the conversation. The path is workspace-relative. Files "
        "written to the code_executor OUTPUT_DIR are already delivered "
        "automatically — use this for other files (reports written with "
        "write_file, test scripts, shell output). Only files inside the project "
        "workspace can be delivered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path of the file to deliver, e.g. reports/summary.csv",
            },
        },
        "required": ["path"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        path = str(params.get("path", "")).strip()
        if not path:
            return {"error": "path is required"}

        project_root = Path(context.project_fs_path).resolve()
        src = (project_root / path).resolve()
        if not src.is_relative_to(project_root):
            return {"error": f"path escapes the project workspace: {path}"}
        if not src.is_file():
            return {"error": f"file not found: {path}"}

        media_dir = Path(context.conversation_media_dir)
        if src.parent == media_dir.resolve():
            dest = src  # already in the served media directory — no copy
        else:
            media_dir.mkdir(parents=True, exist_ok=True)
            dest = media_dir / f"{uuid.uuid4().hex[:8]}{sanitize_suffix(src.name)}"
            try:
                shutil.copy2(str(src), str(dest))
            except OSError as exc:
                return {"error": f"failed to copy {path}: {exc}"}

        record = {
            "id": uuid.uuid4().hex[:8],
            "url": f"/api/media/{context.project_id}/{context.conversation_id}/{dest.name}",
            "name": src.name[:255],
            "media_type": media_type_for(src.name),
            "size": dest.stat().st_size,
        }
        return {"files": [record]}
