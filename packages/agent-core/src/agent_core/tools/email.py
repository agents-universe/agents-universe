"""Email tool — IMAP mailbox access + SMTP sending.

Servers are configured per user in Settings → Integrations → 邮件服务器
(``email_servers`` table); the tool only ever sees a ``server`` slug — hosts,
ports and the decrypted password come from the row via ``_auth.get_email_server``,
so no credential or host travels through the LLM. Every network operation runs
inside a single ``asyncio.to_thread`` call (connect → operate → logout), the
stdlib ``imaplib``/``smtplib`` clients are blocking, and the resolved password
is scrubbed from every error message and log line before it leaves the tool.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext
from ._auth import (
    EMAIL_NOT_CONFIGURED_HINT,
    ToolAuthError,
    get_email_server,
    list_email_servers,
)
from ._uploads import _clean_name
from .shell import redact_secrets

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 50
_DEFAULT_BODY_CHARS = 20_000
_MAX_BODY_CHARS = 100_000
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# RFC 3501 SINCE dates use English month abbreviations; strftime("%d-%b-%Y")
# follows the process locale and would emit localized months some servers
# reject — build the name by hand.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _redact_error(server: dict[str, Any], text: str) -> str:
    """Scrub the resolved password out of a message bound for the LLM/log."""
    return redact_secrets(text, {"email": server.get("password") or ""})


class _HTMLTextExtractor(HTMLParser):
    """Crude HTML → plain text for HTML-only mail bodies.

    A parser subclass, not a regex: mail HTML carries entities, comments and
    script/style blocks that regexes either miss or mangle.
    """

    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "tr"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.chunks.append(data)

    def text(self) -> str:
        raw = "".join(self.chunks)
        return "\n".join(line.strip() for line in raw.splitlines() if line.strip())


def _html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:
        # Malformed HTML must not sink the read — fall back to the raw text.
        return html
    return extractor.text()


def _imap_literal(value: Any) -> str:
    """Quote a value as an IMAP SEARCH string literal (RFC 3501)."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _since_to_imap(value: str) -> str:
    """ISO ``YYYY-MM-DD`` → RFC 3501 ``SINCE 01-Jan-2026`` form."""
    parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    return f"SINCE {parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year}"


def _build_search(criteria: dict[str, Any] | None) -> str:
    """Compose an IMAP SEARCH key from the tool's criteria object."""
    criteria = criteria or {}
    parts: list[str] = []
    if criteria.get("unseen"):
        parts.append("UNSEEN")
    if criteria.get("from"):
        parts.append(f"FROM {_imap_literal(criteria['from'])}")
    if criteria.get("subject_contains"):
        parts.append(f"SUBJECT {_imap_literal(criteria['subject_contains'])}")
    if criteria.get("since"):
        parts.append(_since_to_imap(criteria["since"]))
    return " ".join(parts) if parts else "ALL"


def _decode_mime_header(value: Any) -> str:
    """RFC 2047 encoded-word header → plain str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _validate_host(server: dict[str, Any], host: str, port: int) -> None:
    """SSRF gate at connect time (same policy as the API's test endpoint).

    Literal-IP / metadata-host checks always run; DNS resolution follows
    SSRF_ENABLED. Internal mail servers opt out per row.
    """
    if server.get("allow_private_network"):
        return
    from ._ssrf import SSRFError, validate_url
    try:
        # https:// wrapper: validate_url only accepts http/https schemes, and
        # the host/metadata/IP checks it runs are scheme-independent.
        validate_url(f"https://{host}:{port}", allow_any_port=True)
        from ._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from ._ssrf import resolve_and_validate
            resolve_and_validate(host, port)
    except SSRFError as e:
        raise RuntimeError(
            f"Blocked mail host {host}: {e}. Enable 允许内网地址 on the server "
            "in Settings → Integrations for internal mail servers."
        ) from e


def _connect_imap(server: dict[str, Any]):
    """Blocking IMAP connect + LOGIN. Caller owns logout."""
    import imaplib

    timeout = int(server.get("timeout_seconds") or _DEFAULT_TIMEOUT)
    host, port, security = server["imap_host"], server["imap_port"], server["imap_security"]
    if security == "ssl":
        client = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    else:
        client = imaplib.IMAP4(host, port, timeout=timeout)
        if security == "starttls":
            client.starttls()
    client.login(server["username"], server["password"])
    return client


def _fetch_message_bytes(client, uid: int) -> bytes:
    """UID FETCH the full message with BODY.PEEK — never marks it seen."""
    typ, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if typ != "OK" or not data or data[0] is None:
        raise RuntimeError(f"Message UID {uid} not found (or fetch failed)")
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and item[1]:
            return item[1]
    raise RuntimeError(f"Message UID {uid}: server returned no payload")


def _parse_message(raw: bytes):
    from email import policy
    from email.parser import BytesParser

    return BytesParser(policy=policy.default).parsebytes(raw)


def _message_bodies(msg) -> tuple[str, list[dict[str, Any]]]:
    """(body_text, attachment_metadata) for a parsed message.

    Attachments are metadata only — payloads never ride into the result.
    """
    body_part = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body_part is not None:
        try:
            text = body_part.get_content()
        except Exception:
            payload = body_part.get_payload(decode=True) or b""
            text = payload.decode("utf-8", errors="replace")
        if body_part.get_content_type() == "text/html":
            text = _html_to_text(text)

    attachments: list[dict[str, Any]] = []
    for index, part in enumerate(msg.iter_attachments()):
        payload = part.get_payload(decode=True) or b""
        attachments.append({
            "index": index,
            "filename": part.get_filename() or "",
            "content_type": part.get_content_type(),
            "size": len(payload),
        })
    return text, attachments


def _attachment_parts(msg) -> list:
    return list(msg.iter_attachments())


def _unique_name(name: str, taken: set[str]) -> str:
    """Deduplicate on collision with ``-2``/``-3``… — never overwrite."""
    if name not in taken:
        taken.add(name)
        return name
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    counter = 2
    while f"{stem}-{counter}{suffix}" in taken:
        counter += 1
    unique = f"{stem}-{counter}{suffix}"
    taken.add(unique)
    return unique


class EmailTool(Tool):
    name = "email"
    prompt_hint = (
        "First stop for any mail task: list/search messages, read bodies, "
        "download attachments into .tmp/mail/ (then parse them with doc_reader), "
        "and send mail — send always asks the user first. Servers are configured "
        "in Settings → 外部集成 → 邮件服务器; call list_servers to discover slugs "
        "and pass only the `server` slug, never a host. When a server is missing, "
        "relay the tool's configuration hint instead of asking for a password."
    )
    description = (
        "Read and send email through user-configured IMAP/SMTP servers: list/search "
        "messages, read message bodies, download attachments to the workspace "
        "(.tmp/mail/<conversation_id>/), and send mail with a user confirmation gate."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "list_servers", "list_messages", "get_message",
                    "download_attachments", "send",
                ],
            },
            "server": {
                "type": "string",
                "description": "Server slug from list_servers (not a host)",
            },
            "folder": {"type": "string", "default": "INBOX"},
            "uid": {"type": "integer", "description": "Message UID (get_message / download_attachments)"},
            "criteria": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "subject_contains": {"type": "string"},
                    "since": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                    "unseen": {"type": "boolean"},
                },
            },
            "limit": {"type": "integer", "default": _DEFAULT_LIST_LIMIT},
            "attachment_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Which attachments to download (default: all)",
            },
            "max_chars": {"type": "integer", "description": "Body cap for get_message"},
            "to": {"type": "array", "items": {"type": "string"}},
            "cc": {"type": "array", "items": {"type": "string"}},
            "bcc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "in_reply_to": {"type": "string", "description": "Message-ID being replied to"},
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params.get("operation")
        try:
            if operation == "list_servers":
                return await self._op_list_servers(context)

            slug = params.get("server")
            if not slug:
                return {
                    "error": "`server` is required — call list_servers first and pass a slug",
                    "hint": EMAIL_NOT_CONFIGURED_HINT,
                }
            try:
                server = await get_email_server(context, str(slug))
            except ToolAuthError as e:
                return {"error": str(e), "hint": EMAIL_NOT_CONFIGURED_HINT}

            handler = getattr(self, f"_op_{operation}", None)
            if handler is None:
                return {"error": f"Unknown operation: {operation}"}
            return await handler(params, server, context)
        except Exception as e:
            # Never let a stray exception escape with an unredacted message.
            password = ""
            try:
                password = (await get_email_server(context, str(params.get("server") or ""))).get("password", "")
            except Exception:
                pass
            message = redact_secrets(str(e), {"email": password})
            _log.warning("email %s failed: %s", operation, message, exc_info=True)
            return {"error": f"Email operation failed: {message}"}

    # -- operations ---------------------------------------------------------

    async def _op_list_servers(self, context: ToolContext) -> dict[str, Any]:
        servers = await list_email_servers(context)
        if not servers:
            return {"servers": [], "hint": EMAIL_NOT_CONFIGURED_HINT}
        return {
            "servers": [
                {
                    "slug": s["slug"],
                    "display_name": s["display_name"],
                    "imap_host": s["imap_host"],
                    "imap_port": s["imap_port"],
                    "username": s["username"],
                    "key_hint": s["key_hint"],
                    "enabled": s["enabled"],
                }
                for s in servers
            ]
        }

    async def _op_list_messages(self, params: dict[str, Any], server: dict, context: ToolContext) -> dict:
        folder = params.get("folder") or "INBOX"
        limit = int(params.get("limit") or _DEFAULT_LIST_LIMIT)
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        search = _build_search(params.get("criteria"))

        def _work() -> list[dict[str, Any]]:
            client = _connect_imap(server)
            try:
                typ, _ = client.select(folder, readonly=True)
                if typ != "OK":
                    raise RuntimeError(f"Cannot select folder {folder!r}")
                typ, data = client.uid("SEARCH", None, search)
                if typ != "OK":
                    raise RuntimeError(f"SEARCH failed for folder {folder!r}")
                uids = (data[0] or b"").split()
                # UIDs ascend with arrival — the last N are the newest.
                window = uids[-limit:][::-1]
                if not window:
                    return []
                fetch_key = ",".join(u.decode() for u in window)
                typ, fetched = client.uid(
                    "FETCH", fetch_key,
                    '(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])',
                )
                if typ != "OK":
                    raise RuntimeError("FETCH of message headers failed")
                # One SEARCH for the whole window beats a per-UID probe
                # (the N+1 roundtrip adds up on busy inboxes).
                unseen_uids: set[bytes] = set()
                typ, unseen_data = client.uid("SEARCH", None, "UNSEEN")
                if typ == "OK" and unseen_data and unseen_data[0]:
                    unseen_uids = set(unseen_data[0].split())
                envelopes: list[dict[str, Any]] = []
                for item in fetched or []:
                    if not isinstance(item, tuple) or not item[1]:
                        continue
                    header_bytes = item[1]
                    # The untagged response line carries the UID: b'12 (UID 42 ...'
                    tag_line = item[0] if isinstance(item[0], (bytes, str)) else b""
                    if isinstance(tag_line, bytes):
                        tag_line = tag_line.decode("utf-8", errors="replace")
                    uid = _uid_from_tag(tag_line)
                    msg = _parse_message(header_bytes)
                    envelopes.append({
                        "uid": uid,
                        "from": _decode_mime_header(msg.get("From")),
                        "to": _decode_mime_header(msg.get("To")),
                        "subject": _decode_mime_header(msg.get("Subject")),
                        "date": str(msg.get("Date") or ""),
                        "message_id": str(msg.get("Message-ID") or ""),
                        "unseen": str(uid).encode() in unseen_uids,
                    })
                return envelopes
            finally:
                try:
                    client.logout()
                except Exception:
                    pass

        try:
            messages = await asyncio.to_thread(_work)
        except Exception as e:
            return self._imap_error(server, "list_messages", e)
        return {"server": server["slug"], "folder": folder, "search": search,
                "count": len(messages), "messages": messages}

    async def _op_get_message(self, params: dict[str, Any], server: dict, context: ToolContext) -> dict:
        uid = params.get("uid")
        if uid is None:
            return {"error": "`uid` is required for get_message"}
        max_chars = int(params.get("max_chars") or _DEFAULT_BODY_CHARS)
        max_chars = max(1, min(max_chars, _MAX_BODY_CHARS))

        def _work() -> dict[str, Any]:
            client = _connect_imap(server)
            try:
                raw = _fetch_message_bytes(client, int(uid))
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
            msg = _parse_message(raw)
            body, attachments = _message_bodies(msg)
            truncated = len(body) > max_chars
            return {
                "uid": int(uid),
                "server": server["slug"],
                "from": _decode_mime_header(msg.get("From")),
                "to": _decode_mime_header(msg.get("To")),
                "cc": _decode_mime_header(msg.get("Cc")),
                "subject": _decode_mime_header(msg.get("Subject")),
                "date": str(msg.get("Date") or ""),
                "message_id": str(msg.get("Message-ID") or ""),
                "body": body[:max_chars],
                "truncated": truncated,
                "attachments": attachments,
            }

        try:
            return await asyncio.to_thread(_work)
        except Exception as e:
            return self._imap_error(server, "get_message", e)

    async def _op_download_attachments(self, params: dict[str, Any], server: dict, context: ToolContext) -> dict:
        uid = params.get("uid")
        if uid is None:
            return {"error": "`uid` is required for download_attachments"}
        indices = params.get("attachment_indices")
        wanted: set[int] | None = None
        if indices:
            try:
                wanted = {int(i) for i in indices}
            except (TypeError, ValueError):
                return {"error": "attachment_indices must be a list of integers"}

        root = Path(context.project_fs_path) / ".tmp" / "mail" / str(context.conversation_id)

        def _work() -> dict[str, Any]:
            client = _connect_imap(server)
            try:
                raw = _fetch_message_bytes(client, int(uid))
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
            msg = _parse_message(raw)
            parts = _attachment_parts(msg)
            if wanted is not None:
                parts = [p for i, p in enumerate(parts) if i in wanted]
                if not parts:
                    return {"error": f"No attachments matching indices {sorted(wanted)}", "uid": int(uid)}
            elif not parts:
                return {"uid": int(uid), "files": [], "note": "Message has no attachments"}

            root.mkdir(parents=True, exist_ok=True)
            root_resolved = root.resolve()
            taken: set[str] = set()
            files: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for index, part in enumerate(msg.iter_attachments()):
                if wanted is not None and index not in wanted:
                    continue
                payload = part.get_payload(decode=True) or b""
                raw_name = part.get_filename() or f"attachment-{index}"
                name = _clean_name(raw_name, fallback=f"attachment-{index}")
                if len(payload) > _MAX_ATTACHMENT_BYTES:
                    skipped.append({
                        "index": index, "filename": name, "size": len(payload),
                        "error": (
                            f"exceeds the {_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB "
                            "attachment cap — ask the sender for a smaller file or a link"
                        ),
                    })
                    continue
                name = _unique_name(name, taken)
                dest = root / name
                # Belt and suspenders after sanitization: the resolved write
                # target must stay under .tmp/mail/<conversation_id>/.
                if not dest.resolve().is_relative_to(root_resolved):
                    skipped.append({"index": index, "filename": name,
                                    "error": "resolved outside the mail directory"})
                    continue
                dest.write_bytes(payload)
                files.append({
                    "attachment_index": index,
                    "filename": name,
                    "path": f".tmp/mail/{context.conversation_id}/{name}",
                    "size": len(payload),
                    "content_type": part.get_content_type(),
                })
            result: dict[str, Any] = {"uid": int(uid), "server": server["slug"], "files": files}
            if skipped:
                result["skipped"] = skipped
            if files:
                result["note"] = (
                    "Saved under .tmp/mail/. Use doc_reader to parse office/PDF files; "
                    "deliver_file to hand a file to the user."
                )
            return result

        try:
            return await asyncio.to_thread(_work)
        except Exception as e:
            return self._imap_error(server, "download_attachments", e)

    async def _op_send(self, params: dict[str, Any], server: dict, context: ToolContext) -> dict:
        to = [str(x) for x in (params.get("to") or [])]
        cc = [str(x) for x in (params.get("cc") or [])]
        bcc = [str(x) for x in (params.get("bcc") or [])]
        if not (to or cc or bcc):
            return {"error": "`to` (or cc/bcc) is required for send"}
        subject = str(params.get("subject") or "")
        body = str(params.get("body") or "")
        in_reply_to = params.get("in_reply_to")

        # Confirmation gate BEFORE any SMTP I/O — same fail-closed semantics
        # as api_request's write gate (api_request.py:732-759).
        session = getattr(context, "session", None)
        if session is None:
            return {
                "error": "Confirmation required but no interactive session is available",
                "cancelled": True,
            }
        prompt_id = str(uuid_mod.uuid4())
        try:
            confirm = await session.request_user_selection(
                prompt_id=prompt_id,
                field_key=f"email_send_{prompt_id}",
                question=(
                    f"Send email via {server['slug']} to {', '.join(to)}"
                    + (f" (cc: {', '.join(cc)})" if cc else "")
                    + f" — subject: {subject!r}?"
                ),
                kind="selection",
                options=[
                    {"label": "Allow", "value": "allow"},
                    {"label": "Deny", "value": "deny"},
                ],
                allow_other=False,
                task_id=context.current_task_id,
                timeout=120.0,
            )
        except RuntimeError:
            return {"error": "Confirmation prompt timed out", "cancelled": True}
        if confirm != "allow":
            return {"error": "Send denied by user", "cancelled": True}

        def _work() -> None:
            import smtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg["From"] = server["username"]
            if to:
                msg["To"] = ", ".join(to)
            if cc:
                msg["Cc"] = ", ".join(cc)
            msg["Subject"] = subject
            if in_reply_to:
                msg["In-Reply-To"] = str(in_reply_to)
                msg["References"] = str(in_reply_to)
            msg.set_content(body)
            # Bcc lives in the model only — send_message excludes it from the
            # transmitted headers while still delivering to those recipients.
            if bcc:
                msg["Bcc"] = ", ".join(bcc)

            timeout = int(server.get("timeout_seconds") or _DEFAULT_TIMEOUT)
            host, port, security = server["smtp_host"], server["smtp_port"], server["smtp_security"]
            if security == "ssl":
                with smtplib.SMTP_SSL(host, port, timeout=timeout) as client:
                    client.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=timeout) as client:
                    if security == "starttls":
                        client.starttls()
                    client.send_message(msg)

        _validate_host(server, server["smtp_host"], server["smtp_port"])
        try:
            await asyncio.to_thread(_work)
        except Exception as e:
            message = _redact_error(server, str(e))
            _log.warning("email send failed: %s", message)
            return {"error": f"SMTP send failed: {message}", "server": server["slug"]}
        return {"success": True, "server": server["slug"], "to": to, "cc": cc, "subject": subject}

    # -- helpers ------------------------------------------------------------

    def _imap_error(self, server: dict, operation: str, exc: Exception) -> dict[str, Any]:
        message = _redact_error(server, str(exc))
        _log.warning("email %s failed: %s", operation, message)
        return {"error": f"Email {operation} failed: {message}", "server": server["slug"]}


def _uid_from_tag(tag_line: str) -> int:
    """Extract the UID from an untagged FETCH response line."""
    marker = "UID "
    idx = tag_line.find(marker)
    if idx == -1:
        return -1
    digits = ""
    for ch in tag_line[idx + len(marker):]:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else -1
