---
slug: "integration/email-reader"
description:
  zh-CN: "通过 email 工具访问配置好的 IMAP/SMTP 邮件服务器：发现可用邮箱、按条件搜索邮件、读取正文、下载附件到工作区并用 doc_reader 解析；发信一律走确认门"
  en-US: "Access configured IMAP/SMTP mailboxes through the email tool: discover servers, search messages, read bodies, download attachments into the workspace for doc_reader parsing; sending always goes through the confirmation gate"
triggers:
  - "email"
  - "inbox"
  - "read email"
  - "check inbox"
  - "send email"
  - "邮件"
  - "收件箱"
  - "查邮件"
  - "读邮件"
  - "发邮件"
tools:
  - email
  - doc_reader
  - filesystem
  - deliver_file
  - user_confirm
---

# Skill: Email Reader

Read and act on mailboxes configured in Settings → 外部集成 → 邮件服务器. Credentials live only in the `email_servers` table (user-scoped, AES-256-GCM encrypted) — they never appear in chat, knowledge, or tool results.

## Trigger Conditions

- The user asks to check, read, search, or summarize email / the inbox.
- The user asks to download attachments from email (resumes, reports, invoices…).
- The user asks to send or reply to an email.
- A workflow needs mailbox intake as its first step (e.g. resume screening).

## Execution Steps

1. **Discover servers first**: `email(operation="list_servers")` → returns each server's `slug`, `display_name`, `imap_host`, `username`, `key_hint`. If the user did not specify a mailbox and several exist, ask which one via `user_confirm`.
2. **Search**: `email(operation="list_messages", server="<slug>", criteria={...}, limit=N)` — envelopes only (uid, from, subject, date, flags). Criteria: `from`, `subject_contains`, `since` (ISO date), `unseen`. Start narrow (`unseen: true` or a `since` window); do not page the whole mailbox.
3. **Read a message**: `email(operation="get_message", server="<slug>", uid=N)` → plain-text body (HTML stripped) + attachment metadata. Reading never marks messages as seen (BODY.PEEK).
4. **Download attachments**: `email(operation="download_attachments", server="<slug>", uid=N)` → files land in `.tmp/mail/{conversation_id}/`; the result lists workspace-relative paths and any skipped files (oversize). Only download attachments the task actually needs.
5. **Parse documents**: for `.docx` / `.pdf` / `.xlsx` / `.txt` files, call `doc_reader(operation="read", path=".tmp/mail/{cid}/<file>")` (use `info` first on large PDFs/workbooks to triage). `.pptx` and other binary formats are not readable by doc_reader — say so instead of guessing.
6. **Send / reply**: `email(operation="send", server="<slug>", to=[...], subject=..., body=..., cc=..., bcc=..., in_reply_to=...)`. A confirmation card appears BEFORE anything is sent — summarize recipients and subject on the card. `cancelled: true` means the user denied or the session had no interactive channel; report it, never retry silently.
7. Quote mail content into the reply as needed; keep the `.tmp/mail/` paths when the user may want to download parsed files via `deliver_file`.

## Output Requirements

- Search results: a compact table (uid, date, from, subject, has-attachments flag).
- Message reads: sender/date/subject header + body summary; list attachments with filenames and sizes.
- Downloads: the exact workspace-relative paths so later steps (doc_reader, deliver_file) can use them.
- Never echo passwords, connection strings, or raw server errors containing credentials (the tool already redacts; do not work around it).

## Error Handling

- **Not configured** — `list_servers` returns an empty list with a hint, or a tool call fails with "邮件服务器 '<slug>' 未配置。尚未配置邮件服务器。…". Relay the hint verbatim and give the configuration steps: Settings → 外部集成 → 邮件服务器 → 添加，填 IMAP/SMTP 主机、端口、TLS 方式与用户名密码（可配置多台）。**Never ask the user to type a password into the chat** — the settings form is the only place credentials are entered.
- Search or fetch fails with an auth error → the stored password is likely wrong or expired; tell the user to update it via 编辑 in the same settings section (password field, save). Do not attempt to re-collect it.
- Connection timeout / network error → show the redacted error, suggest checking host/port/TLS mode, and offer the 测试 button path in settings.
- Attachment skipped (oversize) → report the filename and size limit; suggest the user forward a smaller file or share it another way.
- Send returns `cancelled` → respect it. Ask the user whether to retry; never loop.

## Limitations

- Read-only by default: no delete, no move, no flag changes (all fetches use BODY.PEEK).
- HTML-only attachments are not parsed by doc_reader.
- Gmail requires an app-specific password (regular account password is rejected by Google).

## Credentials

```yaml
credentials:
  - service_key: email_servers
    scope: user
    required: true
    description: IMAP/SMTP mailboxes configured in Settings → 外部集成 → 邮件服务器 (per-user, multiple allowed; password stored encrypted, never shown)
```

Servers are managed in Settings → 外部集成 → 邮件服务器 (add / edit / test / delete). The `email` tool selects one by `slug`; there is nothing to paste into the chat.
