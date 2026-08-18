---
slug: "testing/jira-test-case-manager"
description: "Read test design from a Jira comment, create a test card, and link it to the target requirement card"
---

# Skill: Jira Test Case Manager

If `workflows/test-artifact-and-jira-conventions.workflow.md` is not loaded for the current task, read it first.
Ensure `workflows/automation-workflow-playbook.workflow.md` is loaded — the single-issue automation phase order, trimming rules, and default writeback requirements come from that common document; this skill only adds execution details for the test-card management phase.

- Only skip test-card creation or result writeback when the user explicitly asks for local-only or no-Jira.
- If the user did not explicitly narrow the scope, do not stop after writing results only to the target requirement card.

## Triggers

- The workflow enters the test-card flow stage.
- The user asks to materialize the test design into a Jira test card.
- A structured and readable test-design comment already exists and must be converted into executable test assets.

## Goal

Convert the test design in the target issue comment into a Jira test card, automation-script input, and target-issue linkage, then write execution results back to the test card after the run.

## Execution Steps

1. If the target issue has no structured test-design comment yet, write one first. Then read the comments back immediately, confirm the latest comment's body, comment id, and structure, and treat it as the sole input source for the downstream flow. Prefer the latest comment that is both recent and structurally complete, and enter the test-card flow only after readback verification.
2. Generate the test-card title. Default recommended format: `[AI test][UI][<TARGET-KEY>] <summary>`. For pure API scenarios use `[AI test][API][<TARGET-KEY>] <summary>` and pass `test_kind="api"` to `jira(operation="create_test_issue", ...)`.
3. Create the Jira test card:
  - Issue type: the system injects `JIRA_TEST_ISSUE_TYPE` from integration settings / environment variables; when unset the tool defaults to `Test`. Do not read it from knowledge files or invent a value.
  - Description = stable, concise execution contract: target issue and link, business test scope, reusable actions and expected outcome, data dependencies, pointers to the generated test assets. Detailed UI/API/DB parameters, scripts, assertions, and evidence stay in the structured test design and generated artifacts; a specific technical detail enters Jira only when needed to reproduce or explain a defect.
  - Steps written as requests or actions; the main execution-result body does not go into the description.
  - Self-adapt DB service steps: prefix the source line with `[SELF-ADAPT-DB]` so the framework renders it in red in Jira.
4. Link the test card to the target issue. The system injects `JIRA_TEST_LINK_TYPE` from integration settings / environment variables; when unset the tool defaults to `Tests`. Do not read it from knowledge files.
5. Minimal technical anchors or artifact pointers may be written back into the description or comment; the complete execution result lives in the structured test design, execution artifact, or generated assets.
6. Evidence handling:
  - Upload real system UI screenshots or other key visual evidence from an actual product page/dialog to the test card, always via `jira(operation="add_attachment", ...)`, one file per request, and explain in the comment which case or scenario each belongs to.
  - Every executed UI case needs at least one real-system-UI screenshot and one recording attachment. API-only and file-only cases must not fabricate screenshots to satisfy a visual evidence checklist.
  - File cases: upload the exact original input file used by the test. JSON API cases: save the exact JSON request body as a `.json` file and upload it (no secrets in the saved request evidence). File or JSON response results: upload the output/result file, saving JSON responses as `.json`.
7. If one screenshot contains multiple assertion points, state points, or error points, first use the `screenshot-annotator` skill to generate a focus template and an annotated screenshot, then upload the annotated screenshot so reviewers can read it unambiguously.
8. After a successful run: write the execution summary to a test-card comment and transition the card to done with `jira(operation="transition_issue", issue_key="...", transition_name="Task Done")`. The recommended transition name is `Task Done` — first call `jira(operation="get_transitions", ...)` if the exact name for your Jira instance is uncertain.
9. After a failed run: also create or update the corresponding Bug — title starts with `[AI bug]`, by default assigned to the target requirement card's assignee, and containing at least the failed scenario, detailed reproduction steps, related APIs and parameters, expected result, actual result, evidence links, and links back to the target requirement card and the test card.
10. An execution-summary comment on the target requirement card may only summarize or backlink to the test-card result; it cannot replace the complete execution record and evidence on the test card.

## Jira Writing Format

- Test-card titles, description headings, and result/update comments use the same leading marker as the summary: `[AI test][UI][<TARGET-KEY>]` (UI or UI+integration) or `[AI test][API][<TARGET-KEY>]` (pure API). Do not invent adjacent markers such as `[AI test update result]`.
- Prefer Jira wiki syntax over GitHub Markdown when writing through Jira fields: `h2.` / `h3.` headings, `*` bullets, `#` numbered steps, `||header||` tables. Avoid `##`, long Markdown tables, and long backtick-heavy paragraphs.
- Put the result or current status in the first visible paragraph; a reader should know at a glance whether the item is `passed`, `failed`, `blocked`, or `pending`.
- Use a *Business Summary + Minimal Execution Contract*: business scenario, status, outcome, customer/business impact, evidence, Bug, and next action, plus only the minimum execution detail needed to locate or reproduce the result. Do not default to full JSON, every API parameter, complete script paths, or long logs.
- Separate historical results from the current one: an old run that used historical sample data stays historical evidence only and does not represent the updated scenario.
- Prefer short status tables in summary comments: scenario status, current execution result, historical result if relevant, linked Bug if relevant, next action.
- Keep descriptions stable and concise: scope, data-preparation rule, case table, reusable steps, APIs, evidence requirements; no long narrative execution summaries.
- A comment written with a wrong marker or unclear result is not deleted or overwritten; add a corrected follow-up with the standard `[AI test][UI|API][<TARGET-KEY>]` marker stating which earlier comment it supersedes.
- Always read back the Jira issue or comment after writing and verify the marker, first-paragraph result, table rendering, and Chinese text readability before continuing.

## Output Constraints

- Do not bypass the comment and reconstruct test scope directly from the Jira description.
- Windows / PowerShell: do not pass multi-line Markdown or long Chinese-rich bodies as long CLI arguments; use the `jira` tool's `description` or `comment_body` parameter directly.
- Read long bodies back immediately after submission; if garbled, truncated, or only the first line was written, stop the downstream flow and fix the body first.
- The test card must explicitly reference the target issue key and the source comment; scenario descriptions in the test-design comment, test-card description, execution-result comment, and Bug must all include the target-issue link.
- Every case keeps executable steps in the structured test design and generated assets — for API calls, the request method, endpoint path, and key query/path/body parameters. Jira-facing text may reference the contract without reproducing every parameter; never write only an unexplained API name.
- Self-adapt DB calls: explain why UI and the system's own API were not enough, and mark the affected lines with `[SELF-ADAPT-DB]` or `[DB-SERVICE]`.
- Description focuses on request, steps, and scope; the comment on scenario, result, and evidence. Do not make them identical.
- When the user asks to revise an existing card's body, update the description directly to solidify the latest test scope, execution notes, or corrected structured content; do not default to comment-only appends.
- Results are written back after a successful run; failures are also written back, but do not automatically complete the test card.
- Missing scenario-named screenshot or recording attachments block the final writeback: generate them first or record the problem as a script or process defect. Real system UI screenshots must be uploaded, not only mentioned as local paths.
- A failure exposing a product defect (not a script, environment, or intermittent data problem) requires a Bug, with its key included in the test-card comment.
- No synthetic screenshots from HTML summaries, JSON dumps, local reports, or fabricated pages; screenshots capture only the product/system UI surface under test.
- Input files, JSON request bodies, output files, and JSON response bodies are first-class evidence and must be attached when the scenario uses or produces them; screenshot/video evidence is not a substitute.
- Evidence belongs to its own case or scenario only: no mixing multiple cases' videos/screenshots in one trailing comment block, no cross-case evidence use.
- Multi-point screenshots: upload the annotated version first; the raw image alone is not enough when focus needs explanation.
- Attachment filenames reflect the test scenario; no default names such as `video.webm`.

Recommended test-card description sections:

- `[AI test][UI|API][<TARGET-KEY>]` conclusion / current status
- Target Issue
- Source Comment
- Test Scope
- Test Steps
- APIs / Objects
- Minimal Technical Anchors (only when needed to locate or reproduce a defect)
- Artifact Pointers (only the necessary links or paths to structured test design and generated assets)

Full fields (API parameters, script contents or paths, execution notes, assertions, logs, full results) remain in the structured test design, execution artifacts, or generated assets; Jira keeps only the pointers needed for traceability, reproduction, and evidence review.

Current implementation notes:

- Use the `jira` tool for all Jira operations. Authentication is handled automatically.
- `jira(operation="add_attachment", issue_key="<TEST-KEY>", file_path="<relative-path>")` — invoke separately for each file.
- `jira(operation="update_assignee", issue_key="<KEY>", assignee_account_id="<ID>")` or `assignee_name="<name>"` only when assignment is part of the current workflow.
- `jira(operation="transition_issue", issue_key="<KEY>", transition_name="<name>")` only after confirming the intended transition through `get_transitions`.
- Evidence upload: attachments first, then a comment summarizing the execution with references to the uploaded files; after a screenshot upload succeeds, the comment still explains which case or scenario it belongs to.
- Execution-result writebacks record more than pass/fail: business result, impact, evidence, Bug status, next action, plus the minimum key step or assertion for traceability; complete API calls, parameters, assertions, script output, and logs stay in the execution artifact or evidence, not by default in Jira prose.
- After creating a failure Bug, read it back immediately and verify the body is complete and linked correctly.

## Recommended Tool Calls

```json
jira(operation="get_comments", issue_key="<JIRA-KEY>")
jira(operation="get_transitions", issue_key="<TEST-ISSUE-KEY>")
jira(operation="create_test_issue", target_issue_key="<JIRA-KEY>", summary="[<JIRA-KEY>] <title>", description="<content>", labels=["AITest"])
jira(operation="link_issues", from_key="<TEST-KEY>", to_key="<JIRA-KEY>", link_type="<LINK-TYPE>")
jira(operation="update_assignee", issue_key="<JIRA-KEY>", assignee_account_id="<ID>")
focus_template(image_path="tests/generated/artifacts/example.png", count=2, units="percent")
image_annotator(image_path="tests/generated/artifacts/example.png", focus_areas=[...])
jira(operation="add_attachment", issue_key="<TEST-KEY>", file_path="tests/generated/artifacts/example-annotated.png")
jira(operation="transition_issue", issue_key="<TEST-KEY>", transition_name="Task Done")
jira(operation="add_comment", issue_key="<TEST-KEY>", comment_body="<execution result summary>")
```

## Integration With Script Generation

- Input to `playwright-generator` should come primarily from the test-card description and the target issue's test-design comment.
- If the comment is updated after the test card has been created, compare the differences and decide whether to update the test card or regenerate the script.
