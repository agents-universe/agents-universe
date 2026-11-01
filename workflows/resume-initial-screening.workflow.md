---
slug: "resume-initial-screening"
description:
  zh-CN: "招聘助理工作流：确认筛选标准 → 校验邮件集成 → 收件下载简历 → doc_reader 解析 → 四维评分引证据 → 模板汇总交付；回复/转发是独立显式请求"
  en-US: "Recruiting workflow: confirm screening rubric → verify email integration → intake and download resumes → parse with doc_reader → score with cited evidence → deliver the templated summary; replies/forwards are separate explicit requests"
agent: "recruiting-assistant"
triggers:
  - "简历初筛"
  - "初筛这批简历"
  - "screen incoming resumes"
  - "initial resume screening"
tools:
  - email
  - doc_reader
  - filesystem
  - knowledge_rw
  - user_confirm
  - deliver_file
---

# Resume Initial Screening

Use this workflow when the user asks to screen a batch of resumes that arrived by email. Follow the sequence exactly; each step gates the next.

## 1. Confirm the screening standard

1. Read `agents/skills/screening/resume-screening.md` first. Look for an existing rubric or job description in project knowledge (`knowledge_rw`).
2. No rubric in knowledge → draft the four dimensions (skill fit / experience depth / education / language) plus threshold and confirm with `user_confirm` before scoring anything. Never switch the rubric mid-batch.

## 2. Verify email integration

1. Read `agents/skills/integration/email-reader.md`. Call `email(operation="list_servers")`.
2. Empty list or "未配置" error → relay the tool's configuration hint verbatim (Settings → 外部集成 → 邮件服务器: IMAP/SMTP host, port, TLS, username + password; multiple servers supported) and **stop the workflow**. Never ask for a password in the conversation.
3. Several servers → `user_confirm` which mailbox slug to use.

## 3. Intake

1. Read `agents/skills/screening/email-intake.md`. Search with narrow criteria: `unseen: true` plus `subject_contains` recipes (简历 / 应聘 / resume / application, one term per call) and/or `since` for the agreed window.
2. Triage envelopes; keep application mails. Note `has_attachments`.
3. Download attachments for kept uids only (`email(operation="download_attachments")`). Skip executables and anything reported as skipped; record skip reasons.
4. Dedupe by `message_id` (fallback uid + date + subject) against this conversation's earlier intake.

## 4. Parse

1. For each attachment path: `doc_reader(operation="info", path=...)` on large PDFs/workbooks first, then `doc_reader(operation="read", path=...)`.
2. `.docx` resumes (the common case) parse directly. `.pptx`/image-only PDFs/encrypted PDFs → mark `unreadable` with the reason; do not guess contents.
3. Record the parsed-text availability per candidate.

## 5. Score

1. Score every readable candidate on the step-1 rubric, per `screening/resume-screening`: each dimension 1–5 with a verbatim quote; gaps `[inferred]` / `not stated`; anti-bias guardrails apply — never use or mention protected attributes.
2. Overall = mean to 1 decimal; shortlist at the confirmed threshold.

## 6. Report and deliver

1. Read `agents/skills/screening/candidate-summary.md` and write `reports/screening-{YYYY-MM-DD}.md` from the template (overview / score table / shortlist / open questions / attachment paths / unreadable list).
2. `deliver_file` the report. In the chat, give the shortlist names, total counts, and the download link quoted verbatim from the tool result.

## 7. Follow-ups (separate requests only)

Replying to or forwarding any candidate mail is NOT part of this workflow. Only when the user explicitly asks afterwards, run `email(operation="send", ...)` — a confirmation card appears before anything is sent; `cancelled: true` means the user denied it, which is final for that attempt.
