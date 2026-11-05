---
slug: "automation-workflow-playbook"
description:
  zh-CN: "Quality Assurance 智能体的默认工作流参考——阶段顺序、闭环规则、发布回归流程与 Windows/PowerShell 约定"
  en-US: "Default workflow reference for the quality-assurance agent — phase order, closed-loop rules, release regression flow, and Windows/PowerShell conventions"
triggers:
  - "Jira test design"
  - "automation generation"
  - "release regression"
  - "execution validation"
  - "result writeback"
tools: []
---

# Common: Automation Workflow Playbook

This is the `quality-assurance` agent's default workflow reference. For tasks involving Jira test design, automation generation, execution validation, result writeback, or release regression, follow the phase order and closed-loop rules here first, then layer on project knowledge and the phase-specific skills. `workflows/test-artifact-and-jira-conventions.workflow.md` defines Jira writing, card linking, evidence naming, and the comment vs. description split; both apply together.

Language governance: `workflows/` files are repository-wide framework documents and stay English regardless of `AGENT_DEFAULT_LANGUAGE`. Project-scoped assets (`projects/{project}/knowledge/**`) and project Jira prose follow `AGENT_DEFAULT_LANGUAGE` unless the user requests otherwise.

## 1. General Principles

- Default workflow reference, not a runtime pipeline — but a single-issue task runs the full §2 closed loop, Steps 1–9, **including Step 7 (`test_generator` script generation)**.
- Trim phases only when the user explicitly says local-only, no-Jira, or requests only one segment. Any skipped phase — Step 7 above all — must be named with its reason in the final reply to the user. A run that ends without saying what was skipped is incomplete, no matter what else it produced.
- Infer the active project first, then read `projects/{project}/knowledge/`; ask only if it cannot be inferred.
- Designing test cases for a Jira card requires reading the card (issue + comments + transitions) and its linked PRs from the remote (`jira` `get_issue_context`, then `github` `get_pr_details` with the card's key — diff, reviews, comments, checks) — never design from the Jira description alone; local Git history is a supplement. The batch calls remove round trips, never the reads.
- Independent reads that serve one decision belong in ONE assistant turn (the card bundle, its PRs, the knowledge files for the same question) — they run concurrently. One batched call beats a loop of single-item calls.
- Verification-source priority is fixed: UI -> system-owned API -> self-adapt DB access service. Use the DB access service only as the last fallback.
- **Black-box test model**: every card is verified from the outside — real user entry path (UI) or the system's own API; never by running the checked-out product repo's unit/component tests (pytest, vitest/jest, `npm test`, etc.), and their results are never evidence. The repo checkout exists for Git change-scope analysis only; the only executed tests are the agent's own generated Playwright specs in the project workspace `tests/`.
- UI navigation priority: real UI entry -> real clicks -> route/assertion checks. Do not shortcut to a deep link or `page.goto(...)` when the feature is reachable from the live UI, except explicit direct-route negative checks after menu visibility has been validated or when no UI entry exists.
- PR review, approve, and merge are not the `quality-assurance` agent's job; route those requests to `tech-lead`.
- Windows/PowerShell: prefer UTF-8 file-backed payloads for long Jira bodies instead of very long command-line arguments.

### Windows / PowerShell Chinese Text Handling

- Keep Chinese UI text in a UTF-8 file instead of passing it raw through `tsx -e`, `tsx --eval`, or other inline command arguments.
- If inline execution is unavoidable, switch the session to UTF-8 first: set `chcp 65001`, `[Console]::InputEncoding`, `[Console]::OutputEncoding`, and `$OutputEncoding` in the same session.
- Treat `????` in terminal-pasted selectors or assertions as a local shell/code-page problem first, not as corruption of the product UI, Jira body, or repository file.
- For durable automation code, use stable Unicode escapes for Chinese selectors and fixed UI anchors likely to be copied through Windows terminals, avoiding code-page corruption in ad hoc edits, terminal echo, and inline execution.
- Use file-backed payloads and temporary scripts for content that mixes long text and Chinese, including Jira comments, Playwright probes, and API debug snippets.

## 2. Standard Single-Issue Closed Loop

1. Read project knowledge and confirm the active project.
2. Read the target Jira card, related issues, and acceptance criteria (`jira` `get_issue_context` — issue + comments + transitions in one call).
3. If knowledge is insufficient, read the card's linked PRs in one call (`github` `get_pr_details` with the card's key → diffs, reviews, comments, checks); only then fill remaining gaps with local Git history, optional Confluence, the target application, and Kong / OpenAPI context.
4. Design structured test cases: prefer UI or UI+integration, and retain related APIs, key parameters, scripts, assertions, and evidence requirements in the structured design and generated assets. Resolve each case's data-setup need now, not at execution time (see `testing/test-data-setup`).
5. Write a business-readable test design summary plus the minimum execution contract to a target Jira comment, then read back **the comment you just wrote** (`jira(operation="get_comment", issue_key="...", comment_id="<id returned by add_comment>")`) to confirm completeness and usability — no need to re-read the whole comment list. Do not default to pasting full JSON, every API parameter, long logs, or complete script output into Jira.
6. Create or update a linked `[AI test]` test card based on the confirmed design comment — **one card per target issue and test kind (UI/API) across runs**: reuse and update the existing card when one exists (`create_test_issue` returns `status: "already_exists"` with its key); create only when none exists.
7. Generate the Playwright script **with the `test_generator` tool** from the confirmed design comment or test-card scope — never hand-write `.spec.ts`.
   - **First run vs re-run**: check `tests/generated/{slug}.spec.ts` first (`slug` = issue key lowercased, non-alphanumerics → `-`). Missing → first run, generation is **mandatory** (exceptions: user trimmed before Step 7, or §6's no-automatable / API-kind endings). Present → re-run: keep the spec when the design is unchanged; delete the file and regenerate when the design changed or `scaffold_updates` upgraded the config — the tool's `Spec file already exists` refusal is the re-run signal, never a failure and never a reason to stop.
   - **`error` is never swallowed**: a `test_generator` response containing `error` blocks execution and writeback. Input errors (`issue_key and test_cases are required`, `Invalid output_dir`, stringified cases) → fix the input and retry at least once; persistent failure → quote the error verbatim in the final reply and mark generation failed. Success means the response carries `file_path` **and the file exists** (verify via `filesystem`) — state the relative spec path in the summary before moving to Step 8.
8. Execute the script and collect scenario-level screenshots and recordings. Run generated Playwright `.spec.ts` through the platform server-side channel `POST /api/projects/{project_id}/playwright/specs/{slug}/run` (body `{"env": {"APP_*": "..."}}`) and poll `GET /api/scripts/runs/{run_id}`; fall back to agent-side `shell(command="npm run test:{slug}", cwd="tests")` only when the platform API is unreachable (last resort — the shell fallback runs the shared dependency + browser setup). Never hand a TypeScript spec to `code_executor` (`python-playwright` runs Python, not `.spec.ts`); ad-hoc browser checks during execution use `browser_playwright` / `code_executor(language="python-playwright")`, never `shell` node scripts. For UI scenarios, drive the page through the same visible entry points a user would use before asserting. Whenever a screenshot carries multiple assertion points, state points, field comparisons, or error messages, default to an annotated screenshot first. Then validate evidence completeness, classify failures, create or update a Bug only for product defects, and complete Jira writeback.
9. Write new knowledge back to `projects/{project}/knowledge/`, and clean up temporary files created for the run.

## 3. Release Regression Flow

1. Read release metadata and the scoped issue list from a release URL or version id.
2. Infer the active project from release metadata first instead of asking the user immediately.
3. Combine the release scope, project knowledge, and Git context into a complete release regression plan.
4. Present card-level regression coverage and main-flow regression separately, never merged into a single list.
5. Each release issue must either map to at least one regression scenario or enter the untestable list with a specific reason.
6. If Jira asset creation is enabled, create one release summary test card and one corresponding test cycle.
7. Bugs raised by card-regression failures must link to both the release test card and the original issue; bugs raised by main-flow failures must link only to the release test card.
8. Execution writeback must distinguish `passed`, `failed`, `blocked`, and `partially validated`, and include direct attachment links when available.

## 4. Workflow Slices

### Test Design

- Coverage: requirement reading, project context, Git calibration, structured test design.
- Main outputs: structured test cases with complete internal execution fields, plus a business-readable test-scope summary and minimal Jira execution contract.

### Automation Development

- Coverage: generate the Playwright script with the `test_generator` tool (never hand-write `.spec.ts`) from confirmed design and persist Jira test assets.
- Main outputs: generated script and internal execution artifacts, with concise test-card scope and pre-execution notes.

### Regression Validation

- Coverage: execution, annotated evidence, evidence validation, failure classification, Bug handling, Jira writeback, temporary file cleanup.
- Main outputs: execution result, evidence set, failure classification or Bug key, test-card/requirement-card writeback result.

### Release Regression

- Coverage: release scope reading, card-level and main-flow regression design, release-level Jira assets, release-specific Bug linking rules.
- Main outputs: release summary test card, test cycle, card-regression list, untestable-card list, main-flow regression list, SIT-style writeback result.

## 5. Source of Truth

- This document is the human-readable source of truth for the workflow.
- Subtasks (`test-design`, `automation-development`, `regression-validation`, `release-regression`) are created with `jira(operation="create_issue", ...)` / `jira(operation="create_test_issue", ...)`.

## 6. Key Closed-Loop Requirements

- Unless the user explicitly trims the scope, single-issue automation must complete the closed loop `design comment -> test card -> generated spec (Step 7) -> execution -> result writeback -> optional target-issue summary`. **Default = the full loop including Step 7.** Exactly two endings finish without a new spec, and both require an explicit statement in the final reply: (a) the user trimmed the scope before Step 7; (b) zero automatable cases (manual-check-only design) — say why no case could be automated. Silence is not an option.
- **Pure API-kind cards are an artifact branch, not a skip**: when every designed case is API/job/DB (`test_kind="api"`, no UI or UI+integration case), verification runs through the system's own API calls and evidence is request/response `.json` per the artifact conventions — no Playwright spec is expected (the generator emits UI actions only and would produce a run that proves nothing). The report must still say so explicitly: `API-kind card: no spec generated; verification via <API>, evidence = <request/response files>`. A card with **any** UI case is kind UI and requires the spec.
- Evidence must match the test type per scenario. UI scenarios need at least one real product/system UI screenshot and one recording, both renamed per scenario before Jira writeback; unless a screenshot is extremely simple and unambiguous, default to annotated over raw. API-only and file-only scenarios keep request/response or input/output evidence instead and must not fabricate screenshots. Keep applying the existing evidence ownership, naming, attachment, and read-back rules.
- Classify failures before deciding whether to create a Bug. Never automatically treat a script defect, environment timeout, or test-data issue as a product defect.
- Result writeback must land on the test card. A comment on the target requirement card can only be a summary or backlink and cannot replace the complete record on the test card.
