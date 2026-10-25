"""Structured API errors with stable machine-readable codes.

Every raise site produces `HTTPException(detail={"code", "message", ...})`.
The message is an English canonical for logs/unknown-code fallback; the
frontend maps `code` to localized copy in its `errors` namespace and falls
back to the raw message for codes it does not know.

Code conventions: SCREAMING_SNAKE, domain-prefixed —
AUTH_ / PROJECT_ / KNOWLEDGE_ / SCHEDULE_ / TOKEN_ / MCP_ / PUBLISH_ /
UPLOAD_ / WS_ / SCRIPT_ / CONVERSATION_ / MODEL_ / AGENT_ / MEMBER_ /
WORKSPACE_ / INTEGRATION_.
"""
from __future__ import annotations

from fastapi import HTTPException


def api_error(
    status_code: int,
    code: str,
    message: str,
    **extra: object,
) -> HTTPException:
    """Build an HTTPException with a {code, message, **extra} detail object."""
    detail: dict[str, object] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def api_detail(detail: dict) -> dict:
    """Normalize an existing dict detail to guarantee code/message keys exist.

    Used at the few sites that already raise structured details (media upload
    codes etc.) so every error frame carries a consistent shape.
    """
    if "message" not in detail and "detail" in detail:
        detail["message"] = detail.pop("detail")
    detail.setdefault("code", "INTERNAL_ERROR")
    detail.setdefault("message", "Internal server error")
    return detail


def error_message(exc: Exception) -> str:
    """Human-readable message from an exception, handling dict details.

    HTTPException raised by api_error carries a ``{code, message}`` dict —
    ``str(exc)`` would render the whole dict. String details are taken
    verbatim (``str(HTTPException)`` prefixes the status code). Non-HTTP
    exceptions fall back to ``str(exc)`` like before.
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    elif isinstance(detail, str) and detail:
        return detail
    text = str(exc)
    return text or type(exc).__name__
