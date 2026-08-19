---
slug: "test-artifact-and-jira-conventions"
description: "Cross-project convention set for test evidence naming, Jira card writing, result writeback, and the split between comment and description responsibilities"
triggers:
  - "write back to Jira"
  - "upload evidence"
  - "test artifact naming"
  - "screenshot naming"
  - "Jira comment"
  - "test card description"
tools: []
---

# Common: Test Artifact And Jira Conventions

This common document is a cross-project convention set. Whenever a task involves a test-design comment, test-card description, execution-result comment, Bug writeback, screenshot naming, or recording naming, follow these rules first, then layer on project knowledge.

## 0. Permission To Update Jira Bodies

- For the QA automation workflow only: absent remote deletion, clearing, or destructive overwrite, the agent may update existing Jira issue bodies by default to correct, complete, or solidify test design, test scope, execution notes, execution results, and Bug information. This non-destructive default is a QA workflow permission, not a general permission for every agent role.
- Allowed targets include at least an existing issue's description and appended comments; other non-destructive field updates may be used under the same rule when the toolchain supports them.
- When updating an existing Jira issue body, preserve existing links, key information, and traceable context; do not wipe historical execution results indiscriminately. Prefer completion, correction, and structured rewriting over unsupported rewriting of business facts.
- When the user explicitly asks to revise Jira card content, use the available non-destructive update capability directly; comment-only updates are not a default restriction. This does not override another agent role's stricter confirmation guardrail, including the Product Owner's required confirmation before Jira writes.

## 0.5 Standard Single-Issue Closed Loop

- For automation on a single Jira requirement card, the standard closed loop is fixed as: structured design comment on the target issue -> linked `[AI test]` test card -> automated execution -> result writeback on the test card -> optional summary comment on the target issue.
- If the user does not explicitly request local-only or no-Jira, writing execution results only to the target requirement card without creating or updating a test card does not count as a completed workflow.
- The execution comment on the target requirement card is only a summary or backlink supplement. It cannot replace the complete step, result, and evidence archive on the test card.

## 0.6 Whole-System Test Plan Artifact

- The whole-system test plan is a fixed-name design deliverable at `tests/test-plan.md` (project workspace root). It does NOT follow the `{issue-key}-{...}.spec.ts` spec naming pattern.
- In design-only mode it is never uploaded to Jira. If the user later opts in to executing cases from the plan, each executed case follows the evidence, naming, and writeback rules in sections 1-5.

## 1. Scenario-Based Evidence Naming

- Core principle: evidence stays with its own test scenario — never attach scenario A's evidence to scenario B, and never pile attachment links into a summary comment without scenario ownership.
- Screenshot, annotated-image, focus-template, and recording filenames must reflect the test scenario; generic names such as `page.png`, `step.png`, or `video.webm` are not final names.
- Screenshots are only valid evidence for actual system UI pages or dialogs. Never fabricate screenshots, report pages, synthetic HTML dashboards, or image-only summaries to satisfy an evidence checklist. If the scenario is API-only or file-only, attach the request, response, input, and output artifacts instead of creating fake UI screenshots.
- Recommended naming pattern: `<issue-key>/<case-id>-<scenario-slug>.<ext>`.
- Add `-annotated` to annotated images based on the original image, and `-focus` to focus templates.
- `scenario-slug`: the core business action or assertion target from the scenario title, kept short — e.g. `document-list-visible` or `approval-status-updated`.
- Rename Playwright's default `video.webm` to a scenario-meaningful filename before uploading to Jira. Before final writeback, verify that every executed scenario has at least one scenario-named video; if the only current file is `test-results/**/video.webm`, rename it by scenario first and then upload it.
- Whenever a screenshot contains assertion points, state points, field comparisons, button availability, or error messages, default to the annotated image. If both are uploaded, reference the annotated image first; the raw image is only a supplemental archive. Unless a screenshot is extremely simple and cannot be misread, do not upload only the raw image.
- Every executed UI test scenario must produce at least one real system UI screenshot and one recording — never only screenshots or only videos. API-only and file-only scenarios are exempt from screenshot requirements unless they actually open a system UI.
- Upload every executed UI scenario's screenshot and recording to its Jira test card. For a release test card, map scenario, step number, screenshot, and video one by one in the comment.
- If a scenario uses file upload, the exact original input file used by the test is mandatory evidence and must be uploaded to the corresponding Jira test card. Keep the filename scenario-meaningful and make clear that it is the upload input file.
- If a scenario performs a JSON API call, save the exact JSON request body that was sent as a `.json` evidence file and upload it to the corresponding Jira test card. If headers or query parameters are important, document them in the comment; do not put secrets into evidence files.
- If the test result is a file (PDF, Excel, CSV, image, archive, generated template, downloaded or exported report), that result file is mandatory scenario evidence and must be uploaded to the corresponding Jira test card. Keep the filename scenario-meaningful and label it as an output/result file in the comment.
- If the test result is a JSON API response body, save the response body as a `.json` evidence file and upload it to the corresponding Jira test card. Prefer preserving the raw response shape over rewriting it into prose.
- If a scenario downloads or generates a business file, that file is also scenario evidence and must be uploaded to its Jira test card when it exists. Keep the filename scenario-meaningful and list it with that scenario's UI screenshot and recording when UI evidence exists.
- Upload Jira evidence attachments one file at a time — no batching into a single upload request, multipart call, archive, or combined command. Each upload gets its own result handling so a failure is visible and retryable without re-sending unrelated files.
- If one comment reports multiple scenarios, list each scenario's own screenshots and recordings in its own section; never collect them into one shared attachment group.
- When referencing evidence in a test-card comment, default to citing the annotated-image filename plus the recording filename. Only fall back to the raw image when an annotated image does not yet exist, and explain why.
- If a UI scenario produced no screenshot or video because of a script implementation problem, generate the missing artifacts first or explicitly record it as a script defect. A UI execution result with missing screenshot or video is not complete writeback.
- Never replace missing UI evidence with fabricated screenshots. If there is no real system UI involved, say so and attach the applicable non-visual artifacts instead.

## 2. Scenario Descriptions Must Include Issue Links

- Every scenario in a test-design comment, test-card description, execution-result comment, or Bug description must include the corresponding Jira card link.
- Include at least the target requirement card link; keep the test-card or Bug link as well when the current content belongs to one.
- Recommended format: `Target Issue: PROJ-1234 <url>`, or a standalone `Issue Link` under the scenario title.

## 3. Responsibility Of The Test-Card Description

- The test-card description is a stable, concise execution contract. Focus on the target issue, business test scope, reusable actions, expected outcome, data dependencies, and pointers to generated test assets rather than broadcasting results.
- Every case must keep clear request or action steps in the structured test design and generated assets. If APIs are involved, retain the method, path, and key query, path, and body parameters there; Jira prose may reference the relevant contract without reproducing every parameter.
- Steps inside the description should be relatively static, reusable, and reproducible. Avoid turning a one-time execution result into a long-term description.
- Do not default to putting complete JSON payloads, every API parameter, long logs, or full script output in Jira descriptions.

## 4. Responsibility Of Comments

- Jira comments are about scenario plus result, not a full copy of the test-card description.
- Use a *Business Summary + Minimal Execution Contract*: put status, business outcome, customer/business impact, evidence, Bug status, and next action first. Include only the minimum execution detail needed to locate or reproduce the result.
- An execution-result comment should highlight which scenario was executed, whether the result was `passed`, `failed`, `blocked`, or `pending`, key business observations, the mapping between evidence files, and whether a Bug was created.
- Each scenario in a comment must be followed immediately by that scenario's own evidence references, by default organized as: scenario explanation -> UI screenshot/recording when real UI is involved -> upload input file when used -> JSON request body when an API is called -> output/result file when produced -> JSON response body when returned -> supplemental result. Do not detach evidence links from the scenario body and stack them elsewhere.
- For request, response, input, and output evidence, reference the uploaded scenario-owned files. Inline only the key fields needed to reproduce or explain a defect; this is not permission to paste complete JSON into a Jira comment.
- Every test case in an execution-result comment must include a brief description, preferably 1 to 3 sentences explaining what was tested, how it ended, and which evidence to inspect.
- A comment may still keep essential key steps and request details, but only to explain results, locate problems, and connect evidence. It must not replace the description as the full test specification.

## 4.5 Self-Adapt DB Red Marking

- If a scenario, step, or execution note uses the self-adapt DB access service, the affected Jira lines must be highlighted in red.
- Jira bodies are written as Markdown and auto-converted to Jira wiki markup at write time (headings, lists, tables, etc.). For the red marking, the auto-converter passes wiki-markup lines through untouched, so wrap those lines yourself, e.g. `{color:red}[SELF-ADAPT-DB] ...{color}`, in any body passed to `jira(operation="create_test_issue"|"update_description"|"add_comment"|"create_test_cycle", ...)`.
- Use this only for lines that truly depend on the self-adapt DB access service. Do not mark ordinary UI or product-API steps red.

## 5. General Bug Rules

- Bug titles created by AI must start with `[AI bug]`.
- By default, assign the Bug to the current assignee of the target requirement card. Do not guess the assignee.
- If the target requirement card has no assignee, leave the Bug unassigned and state in the test-card comment or Bug body that the target card has no assignee and no automatic assignment was made.
- Bug content must include the target issue link, test-card link, and scenario link so the requirement card, test card, and Bug are traceable in all directions.

## 6. Final Checks Before Writing

- In any Jira description, comment, or Bug body about to be written, confirm there is no broken chain between scenario title, issue link, request steps, result summary, and evidence references.
- In any screenshot or recording filename about to be uploaded, confirm that at least the issue key and scenario meaning are visible.
- Before final writeback, confirm that file-upload scenarios include the original uploaded file, JSON API scenarios include request-body `.json` evidence, file-output scenarios include the produced file, and JSON-response scenarios include response-body `.json` evidence. Do not mark evidence complete by attaching only screenshots or videos when these artifacts exist.
