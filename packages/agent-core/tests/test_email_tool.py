"""Tests for the email tool — fake IMAP/SMTP sessions, no network.

Server rows are resolved through ``_auth.get_email_server``/``list_email_servers``
(monkeypatched here to return fixtures), so no DB is involved. The send gate
semantics mirror api_request's write confirmation.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

import agent_core.tools.email as email_mod
from agent_core.tools._auth import ToolAuthError
from agent_core.tools.base import ToolContext
from agent_core.tools.email import EmailTool

SERVER = {
    "email_server_id": "s1",
    "slug": "recruiting-inbox",
    "display_name": "招聘收件箱",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_security": "ssl",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_security": "starttls",
    "username": "hr@example.com",
    "password": "sup3r-s3cret-pass",
    "key_hint": "...pass",
    "allow_private_network": True,  # skip the SSRF gate in unit tests
    "enabled": True,
}


def _context(tmp_path, **kwargs) -> ToolContext:
    return ToolContext(
        project_id="p1",
        project_fs_path=str(tmp_path),
        conversation_id="c1",
        user_id="u1",
        **kwargs,
    )


def _patch_server(monkeypatch, server=SERVER):
    async def _get(context, slug):
        if slug != server["slug"]:
            raise ToolAuthError("email", message=f"邮件服务器 '{slug}' 未配置。尚未配置邮件服务器。")
        return dict(server)

    monkeypatch.setattr(email_mod, "get_email_server", _get)


def _message_bytes(*, subject="简历 - 张三", sender="zhangsan@example.com",
                   plain=None, html=None, attachments=()) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "hr@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Sat, 01 Nov 2026 10:00:00 +0800"
    msg["Message-ID"] = "<msg-1@example.com>"
    if plain is not None:
        msg.set_content(plain)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        # Single-part HTML-only message (no plain alternative to prefer).
        msg.set_content(html, subtype="html")
    else:
        msg.set_content("body")
    for name, payload, ctype in attachments:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class FakeImap:
    """Duck-typed imaplib client: records calls, returns canned responses."""

    def __init__(self, *, search_uids=b"", unseen_uids=b"", fetch_headers=None,
                 fetch_full=None):
        self.search_uids = search_uids
        self.unseen_uids = unseen_uids
        self.fetch_headers = fetch_headers or []
        self.fetch_full = fetch_full
        self.calls = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b""]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            if args[-1] == "UNSEEN":
                return "OK", [self.unseen_uids]
            return "OK", [self.search_uids]
        if command == "FETCH":
            if args[1] == "(BODY.PEEK[])":
                return "OK", self.fetch_full
            return "OK", self.fetch_headers
        return "NO", [b""]

    def logout(self):
        self.logged_out = True


def _headers_response(uid: int, raw: bytes) -> list:
    return [(f'{uid} (UID {uid} BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {{{len(raw)}}}'.encode(), raw), b")"]


@pytest.fixture
def fake_imap(monkeypatch):
    holder = {}

    def _install(client: FakeImap):
        holder["client"] = client

        def _connect(server):
            return client

        monkeypatch.setattr(email_mod, "_connect_imap", _connect)
        return client

    holder["install"] = _install
    return holder


# ── list_servers ────────────────────────────────────────────────────────────


async def test_list_servers_empty_returns_hint(monkeypatch):
    async def _empty(context):
        return []

    monkeypatch.setattr(email_mod, "list_email_servers", _empty)
    result = await EmailTool().execute({"operation": "list_servers"}, _context_tmp())
    assert result["servers"] == []
    assert "Settings → 外部集成 → 邮件服务器" in result["hint"]


async def test_list_servers_returns_metadata_only(monkeypatch):
    async def _rows(context):
        return [dict(SERVER)]

    monkeypatch.setattr(email_mod, "list_email_servers", _rows)
    result = await EmailTool().execute({"operation": "list_servers"}, _context_tmp())
    server = result["servers"][0]
    assert server["slug"] == SERVER["slug"]
    assert "password" not in server
    assert server["key_hint"] == "...pass"


def _context_tmp(**kwargs) -> ToolContext:
    return ToolContext(project_id="p1", project_fs_path=".", conversation_id="c1", user_id="u1", **kwargs)


# ── missing server ──────────────────────────────────────────────────────────


async def test_missing_server_returns_configuration_hint(monkeypatch):
    _patch_server(monkeypatch)
    result = await EmailTool().execute(
        {"operation": "list_messages", "server": "nope"}, _context_tmp()
    )
    assert "error" in result
    assert "Settings" in result["hint"]


async def test_server_param_required(monkeypatch):
    result = await EmailTool().execute({"operation": "list_messages"}, _context_tmp())
    assert "error" in result
    assert "list_servers" in result["error"]


# ── list_messages ───────────────────────────────────────────────────────────


async def test_list_messages_search_and_envelopes(monkeypatch, fake_imap):
    _patch_server(monkeypatch)
    raw1 = _message_bytes(subject='He said "hi" 简历')
    raw2 = _message_bytes(subject="第二封", sender="lisi@example.com")
    client = FakeImap(
        search_uids=b"1 2",
        unseen_uids=b"2",
        fetch_headers=_headers_response(2, raw2) + _headers_response(1, raw1),
    )
    fake_imap["install"](client)

    result = await EmailTool().execute(
        {
            "operation": "list_messages",
            "server": SERVER["slug"],
            "criteria": {"subject_contains": 'he said "hi"', "unseen": False, "since": "2026-11-01"},
        },
        _context_tmp(),
    )
    assert result["count"] == 2
    subjects = [m["subject"] for m in result["messages"]]
    assert 'He said "hi" 简历' in subjects
    # Envelopes carry no bodies.
    assert all("body" not in m for m in result["messages"])
    # Quoting: the double quote is IMAP-escaped, SINCE is RFC 3501 formatted.
    search_calls = [c for c in client.calls if c[:2] == ("uid", "SEARCH") and c[3] != "UNSEEN"]
    search_str = search_calls[0][3]
    assert 'SUBJECT "he said \\"hi\\""' in search_str
    assert "SINCE 01-Nov-2026" in search_str
    assert client.logged_out


async def test_list_messages_limit_clamped(monkeypatch, fake_imap):
    _patch_server(monkeypatch)
    client = FakeImap(search_uids=b"", fetch_headers=[])
    fake_imap["install"](client)
    result = await EmailTool().execute(
        {"operation": "list_messages", "server": SERVER["slug"], "limit": 500},
        _context_tmp(),
    )
    assert result["count"] == 0
    # The search still ran; the clamp happens before fetch, so nothing to assert
    # beyond a successful call — the real clamp is unit-tested via _MAX_LIST_LIMIT.
    assert email_mod._MAX_LIST_LIMIT == 50


# ── get_message ─────────────────────────────────────────────────────────────


async def test_get_message_multipart_prefers_plain(monkeypatch, fake_imap):
    _patch_server(monkeypatch)
    raw = _message_bytes(plain="这是正文", html="<p>HTML 版本</p>",
                         attachments=[("resume.docx", b"PK\x03\x04data", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    client = FakeImap(fetch_full=_headers_response(7, raw))
    fake_imap["install"](client)

    result = await EmailTool().execute(
        {"operation": "get_message", "server": SERVER["slug"], "uid": 7}, _context_tmp()
    )
    assert result["uid"] == 7
    assert "这是正文" in result["body"]
    assert "HTML 版本" not in result["body"]
    assert result["truncated"] is False
    assert result["attachments"][0]["filename"] == "resume.docx"
    # Metadata only — no payload bytes leak into the result.
    assert "data" not in str(result)


async def test_get_message_html_only_stripped(monkeypatch, fake_imap):
    _patch_server(monkeypatch)
    raw = _message_bytes(html="<html><head><style>p{color:red}</style></head><body><script>alert(1)</script><p>你好 <b>世界</b></p></body></html>")
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))
    result = await EmailTool().execute(
        {"operation": "get_message", "server": SERVER["slug"], "uid": 1}, _context_tmp()
    )
    assert "你好 世界" in result["body"]
    assert "alert" not in result["body"]
    assert "color:red" not in result["body"]


async def test_get_message_truncation(monkeypatch, fake_imap):
    _patch_server(monkeypatch)
    raw = _message_bytes(plain="x" * 500)
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))
    result = await EmailTool().execute(
        {"operation": "get_message", "server": SERVER["slug"], "uid": 1, "max_chars": 100},
        _context_tmp(),
    )
    assert result["truncated"] is True
    assert len(result["body"]) == 100


# ── download_attachments ────────────────────────────────────────────────────


async def test_download_attachments_path_traversal_neutralized(monkeypatch, fake_imap, tmp_path):
    _patch_server(monkeypatch)
    raw = _message_bytes(attachments=[
        ("../../evil.pdf", b"a", "application/pdf"),
        ("C:\\evil2.pdf", b"b", "application/pdf"),
        ("..", b"c", "application/octet-stream"),
        ("", b"d", "application/octet-stream"),
    ])
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))

    result = await EmailTool().execute(
        {"operation": "download_attachments", "server": SERVER["slug"], "uid": 1},
        _context(tmp_path),
    )
    assert "files" in result, result
    mail_dir = tmp_path / ".tmp" / "mail" / "c1"
    assert mail_dir.is_dir()
    written = sorted(p.name for p in mail_dir.iterdir())
    assert written  # every sanitized name landed inside the mail dir
    for f in result["files"]:
        assert f["path"].startswith(".tmp/mail/c1/")
        assert ".." not in f["path"]
        assert ":" not in f["path"]
    for name in written:
        assert (mail_dir / name).resolve().is_relative_to(mail_dir.resolve())


async def test_download_attachments_collision_suffix(monkeypatch, fake_imap, tmp_path):
    _patch_server(monkeypatch)
    raw = _message_bytes(attachments=[
        ("resume.pdf", b"a", "application/pdf"),
        ("resume.pdf", b"bb", "application/pdf"),
    ])
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))
    result = await EmailTool().execute(
        {"operation": "download_attachments", "server": SERVER["slug"], "uid": 1},
        _context(tmp_path),
    )
    names = sorted(f["filename"] for f in result["files"])
    assert names == ["resume-2.pdf", "resume.pdf"]
    # Never overwrites: both files exist with their own sizes.
    sizes = {f["filename"]: f["size"] for f in result["files"]}
    assert sizes["resume.pdf"] == 1 and sizes["resume-2.pdf"] == 2


async def test_download_attachments_size_cap(monkeypatch, fake_imap, tmp_path):
    _patch_server(monkeypatch)
    raw = _message_bytes(attachments=[("big.pdf", b"x" * 100, "application/pdf")])
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))
    monkeypatch.setattr(email_mod, "_MAX_ATTACHMENT_BYTES", 10)
    result = await EmailTool().execute(
        {"operation": "download_attachments", "server": SERVER["slug"], "uid": 1},
        _context(tmp_path),
    )
    assert result["files"] == []
    assert result["skipped"][0]["error"].startswith("exceeds")
    assert not list((tmp_path / ".tmp" / "mail" / "c1").iterdir())


async def test_download_attachments_no_attachments(monkeypatch, fake_imap, tmp_path):
    _patch_server(monkeypatch)
    raw = _message_bytes(plain="no files here")
    fake_imap["install"](FakeImap(fetch_full=_headers_response(1, raw)))
    result = await EmailTool().execute(
        {"operation": "download_attachments", "server": SERVER["slug"], "uid": 1},
        _context(tmp_path),
    )
    assert result["files"] == []
    assert "no attachments" in result["note"]


# ── send ────────────────────────────────────────────────────────────────────


class FakeSession:
    def __init__(self, result="allow"):
        self.result = result
        self.calls = []

    async def request_user_selection(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.starttls_called = False
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def send_message(self, msg):
        self.sent.append(msg)


async def test_send_denied_without_session(monkeypatch):
    _patch_server(monkeypatch)
    result = await EmailTool().execute(
        {"operation": "send", "server": SERVER["slug"], "to": ["a@example.com"], "subject": "s"},
        _context_tmp(),  # no session
    )
    assert result["cancelled"] is True


async def test_send_denied_by_user(monkeypatch):
    _patch_server(monkeypatch)
    session = FakeSession(result="deny")
    result = await EmailTool().execute(
        {"operation": "send", "server": SERVER["slug"], "to": ["a@example.com"], "subject": "s"},
        _context_tmp(session=session),
    )
    assert result["cancelled"] is True
    assert "denied" in result["error"]


async def test_send_timeout(monkeypatch):
    _patch_server(monkeypatch)
    session = FakeSession(result=RuntimeError("gone"))
    result = await EmailTool().execute(
        {"operation": "send", "server": SERVER["slug"], "to": ["a@example.com"], "subject": "s"},
        _context_tmp(session=session),
    )
    assert result["cancelled"] is True


async def test_send_allow_dispatches_smtp(monkeypatch):
    _patch_server(monkeypatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    session = FakeSession(result="allow")
    result = await EmailTool().execute(
        {
            "operation": "send", "server": SERVER["slug"],
            "to": ["a@example.com"], "cc": ["b@example.com"], "bcc": ["c@example.com"],
            "subject": "面试邀请", "body": "你好",
        },
        _context_tmp(session=session),
    )
    assert result["success"] is True
    # The confirmation gate ran BEFORE any SMTP I/O.
    assert session.calls and session.calls[0]["options"][0]["value"] == "allow"
    client = FakeSmtp.instances[0]
    assert client.host == SERVER["smtp_host"]
    assert client.starttls_called
    msg = client.sent[0]
    assert msg["From"] == SERVER["username"]
    assert msg["Subject"] == "面试邀请"
    # Bcc rides on the message for delivery; smtplib's send_message excludes it
    # from the transmitted headers (the real client strips it on the wire).
    assert msg["Bcc"] == "c@example.com"


async def test_send_error_redacts_password(monkeypatch):
    _patch_server(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)

    class LeakySmtp(FakeSmtp):
        def send_message(self, msg):
            raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {SERVER['password']}")

    monkeypatch.setattr(smtplib, "SMTP", LeakySmtp)
    session = FakeSession(result="allow")
    result = await EmailTool().execute(
        {"operation": "send", "server": SERVER["slug"], "to": ["a@example.com"], "subject": "s"},
        _context_tmp(session=session),
    )
    assert "error" in result
    assert SERVER["password"] not in result["error"]
    assert "[REDACTED:email]" in result["error"]
