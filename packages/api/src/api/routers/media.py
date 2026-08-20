"""Media file serving and upload — screenshots, generated images, user attachments.

User attachments are never written to disk: the upload endpoint reads the
bytes into an in-memory store (process-local; uvicorn runs single-worker),
the WS handler reads the content during the turn — and tools can re-read
it via the filesystem tool — then the conversation's entries are dropped
when the turn ends. Only agent-generated images (screenshots, tool outputs)
live on disk under `.tmp/media/`.
"""
from __future__ import annotations

import io
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, authorize_conversation, authorize_project, get_current_user
from api.models.conversation import Conversation
from api.models.project import Project

router = APIRouter(prefix="/api/media")

# Whitelist: filenames may only contain alphanumerics, dash, underscore and dots
# (no leading dot — blocks hidden files)
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".html": "text/html",
    ".htm": "text/html",
}

# --- In-memory upload store -------------------------------------------------
# Uploaded user attachments live here (keyed by conversation) until the agent
# turn consumes them; entries also expire via TTL so abandoned uploads never
# linger. Nothing user-uploaded is ever written to disk.

_UPLOAD_TTL_SECONDS = 60 * 60  # upload → send window
# Hard cap on total in-memory upload bytes across all conversations. Each
# file is individually capped (max_upload_size_mb), but without a total
# ceiling a client could fill memory with many capped uploads.
_MAX_TOTAL_UPLOAD_BYTES = 256 * 1024 * 1024
# Per-conversation cap : the global pool is shared by every user,
# so one user uploading repeatedly could exhaust it and 413 everyone else.
# One conversation is one active turn's worth of attachments — 64MB is far
# beyond any legitimate single-turn upload.
_MAX_CONVERSATION_UPLOAD_BYTES = 64 * 1024 * 1024


@dataclass
class _UploadEntry:
    data: bytes
    expires_at: float
    stored_at: float  # upload time — lets a turn boundary drop only prior uploads


_uploads: dict[tuple[str, str], _UploadEntry] = {}  # (conversation_id, filename) -> entry


def _sweep_expired() -> None:
    now = time.time()
    expired = [k for k, e in _uploads.items() if e.expires_at <= now]
    for k in expired:
        _uploads.pop(k, None)


def store_upload(conversation_id: str, filename: str, data: bytes) -> None:
    """Keep upload bytes in memory until the turn consumes them."""
    _sweep_expired()
    total = sum(len(e.data) for e in _uploads.values())
    if total + len(data) > _MAX_TOTAL_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Upload store is full — send the current message or wait for it to expire, then retry",
        )
    conv_total = sum(
        len(e.data) for (cid, _fname), e in _uploads.items() if cid == conversation_id
    )
    if conv_total + len(data) > _MAX_CONVERSATION_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Too many uploads for this conversation — send the message or wait for them to expire, then retry",
        )
    _uploads[(conversation_id, filename)] = _UploadEntry(data, time.time() + _UPLOAD_TTL_SECONDS, time.time())


def _inline_safety_headers(suffix: str) -> dict[str, str]:
    """Headers that keep a served file from executing on the app origin.

    HTML served inline runs on the same origin with the user's session
    cookies - any agent-generated (or upload-stored) markup becomes a stored
    XSS vector. Force a download instead; images keep their inline display.
    """
    if suffix in (".html", ".htm"):
        return {"Content-Disposition": "attachment"}
    return {}


def get_upload(conversation_id: str, filename: str) -> bytes | None:
    _sweep_expired()
    entry = _uploads.get((conversation_id, filename))
    if entry is None:
        return None
    if entry.expires_at <= time.time():
        _uploads.pop((conversation_id, filename), None)
        return None
    return entry.data


def list_upload_names(conversation_id: str) -> list[str]:
    """Filenames currently held for a conversation (tool discovery)."""
    _sweep_expired()
    return [fname for (cid, fname) in _uploads if cid == conversation_id]


def drop_uploads(conversation_id: str, older_than: float | None = None) -> None:
    """Release a conversation's uploads — called when the turn ends.

    With *older_than*, only entries uploaded before that moment are dropped:
    an upload made during the turn (or while waiting for a reply) survives the
    turn boundary and stays available for the next send, instead of being
    wiped by every turn end while the TTL still promises an hour.
    """
    _sweep_expired()
    for key, entry in [(k, e) for k, e in _uploads.items() if k[0] == conversation_id]:
        if older_than is None or entry.stored_at < older_than:
            _uploads.pop(key, None)


def _image_size_bytes(data: bytes) -> tuple[int, int] | None:
    """Read image dimensions from bytes with Pillow. None on any failure."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height
    except Exception:
        return None


@router.post("/{project_id}/{conversation_id}")
async def upload_media(
    project_id: str,
    conversation_id: str,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
    conversation: Conversation = Depends(authorize_conversation),
):
    """Upload a user attachment (image or file) into memory.

    Bytes are held in the in-memory store until the agent turn consumes
    them — nothing is written to disk. Storage name is a server-generated
    uuid; the client filename is display-only.
    """
    if str(conversation.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if ".." in conversation_id or "/" in conversation_id or "\\" in conversation_id or "\x00" in conversation_id:
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    settings = get_settings()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    # Starlette has already spooled the entire multipart part
    # (>1MB to temp disk) by the time the read-cap below runs — concurrent
    # oversized uploads could fill the server's temp disk before ever being
    # rejected. Reject early on the request Content-Length (the file part can
    # never exceed the full body; 64KB slack covers multipart framing).
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes + 64 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {settings.max_upload_size_mb}MB limit",
        )

    # Sanitize suffix: alphanumeric only, short (e.g. ".png", ".xlsx") — anything
    # weird is dropped so the stored filename stays server-controlled.
    suffix = Path(file.filename or "").suffix.lower()
    if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
        suffix = ""
    storage_name = f"{uuid.uuid4().hex}{suffix}"

    # Read with a size cap: no partial file to clean up, no disk involved.
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {settings.max_upload_size_mb}MB limit",
        )
    store_upload(conversation_id, storage_name, data)

    record = {
        "id": storage_name[:32],
        "url": f"/api/media/{project_id}/{conversation_id}/{storage_name}",
        "name": (file.filename or storage_name)[:255],
        "media_type": _MIME_BY_SUFFIX.get(suffix, file.content_type or "application/octet-stream"),
        "size": len(data),
    }
    if suffix in _IMAGE_SUFFIXES:
        dims = _image_size_bytes(data)
        if dims:
            record["width"], record["height"] = dims
    return record


@router.get("/{project_id}/{conversation_id}/{filename}")
async def serve_media(
    project_id: str,
    conversation_id: str,
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
    project: Project = Depends(authorize_project),
    conversation: Conversation = Depends(authorize_conversation),
):
    if str(conversation.project_id) != project_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if ".." in filename or not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if ".." in conversation_id or "/" in conversation_id or "\\" in conversation_id or "\x00" in conversation_id:
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    # User uploads live in memory until consumed — serve from the store first.
    data = get_upload(conversation_id, filename)
    if data is not None:
        suffix = Path(filename).suffix.lower()
        return Response(
            content=data,
            media_type=_MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
            headers=_inline_safety_headers(suffix),
        )

    # Fallback: agent-generated images (screenshots, tool outputs) on disk.
    from api.paths import resolve_project_fs_path
    try:
        project_path = Path(await resolve_project_fs_path(project_id, db))
    except ValueError:
        raise HTTPException(status_code=404, detail="Project not found")

    media_dir = (project_path / ".tmp" / "media" / conversation_id).resolve()
    expected_base = (project_path / ".tmp" / "media").resolve()
    if not media_dir.is_relative_to(expected_base):
        raise HTTPException(status_code=400, detail="Invalid path")

    media_path = (media_dir / filename).resolve()
    if not media_path.is_relative_to(media_dir):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not media_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = media_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".html": "text/html",
        ".htm": "text/html",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(str(media_path), media_type=media_type, headers=_inline_safety_headers(suffix))
