---
slug: "screening/email-intake"
description:
  zh-CN: "招聘收件 intake：配合 integration/email-reader 从未读收件箱筛选简历邮件、按 message/uid 去重下载附件，不下载可执行文件，未配置邮件集成时给出配置引导"
  en-US: "Recruiting mailbox intake: pair with integration/email-reader to filter resume mails from the unseen inbox, dedupe by message/uid, download attachments (never executables), and relay setup guidance when no email integration is configured"
triggers:
  - "check inbox for resumes"
  - "email intake"
  - "收简历"
  - "查收简历"
  - "下载简历附件"
  - "简历邮件"
tools:
  - email
  - doc_reader
  - filesystem
  - user_confirm
---

# Skill: Email Intake (Screening)

Mailbox intake for the recruiting pipeline. Depends on `integration/email-reader` for the raw email mechanics — read that skill first if not already loaded.

## Trigger Conditions

- The user asks to check the inbox for new resumes / applications.
- The screening workflow reaches its "collect new mail" step.
- The user forwards a batch of application mails and wants them downloaded and parsed.

## Execution Steps

1. **Verify configuration**: `email(operation="list_servers")`. Empty → relay the built-in hint verbatim (Settings → 外部集成 → 邮件服务器 configuration steps) and **stop**. Never ask for a password in chat.
2. **Pick the mailbox**: several servers → `user_confirm` which slug (usually the recruiting one, e.g. `recruiting-inbox`).
3. **Search application mail**: build narrow criteria — typically `unseen: true` plus a `subject_contains` recipe (`简历|应聘|申请|resume|application|cv`, evaluated one term at a time since the tool takes a single substring) and/or `since` (ISO date, usually the last 7 days). Do not dump the entire mailbox.
4. **Triage envelopes**: keep mails whose subject/from indicate an application; note `has_attachments` from the envelope. Mails without attachments but with an inline resume body → still read them (`get_message`), the body may hold the profile text.
5. **Download only needed attachments**: `email(operation="download_attachments", ...)` per selected uid. Skip filenames ending in `.exe`, `.bat`, `.cmd`, `.js`, `.scr`, `.msi`, `.vbs`, `.apk` and anything the tool reports as skipped (oversize) — record the skip reason instead of forcing it.
6. **Dedupe**: build the intake list keyed by `message_id` (fallback: uid + date + subject) so re-running the step never double-counts a candidate. Also dedupe against attachments already under `.tmp/mail/{cid}/` from earlier in the conversation.
7. **Record paths**: every kept attachment gets one line — candidate name (from subject/from), uid, filename, workspace path — ready for `doc_reader` parsing in the next step.

## Output Requirements

- Intake table: candidate / uid / filename / path / status (kept | skipped:reason | duplicate).
- Counts: new mails found, attachments kept, skipped, duplicates.
- If configuration is missing, the reply is ONLY the configuration guidance — no partial download attempts.

## Error Handling

- No email server configured → relay the tool hint (see step 1). This is the expected first-run path; do not improvise a different instruction.
- Auth failure on search → password likely rotated; point the user to 编辑 in Settings → 外部集成 → 邮件服务器 (leave-empty-to-keep semantics).
- `download_attachments` returns `skipped` → report each skipped file with its reason; continue with the rest.
- A message's attachment list is empty → say the mail had no attachments; if the resume appears inline, parse the body instead.

## Guardrails

- Never request mailbox passwords in the conversation — settings UI only.
- Never download or execute suspicious file types (step 5).
- Never mark messages as seen as a side effect of listing (the tool uses PEEK; do not work around it).
