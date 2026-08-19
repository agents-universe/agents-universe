---
slug: "automation-workflow-playbook"
description: "Default workflow reference for the quality-assurance agent — phase order, closed-loop rules, release regression flow, and Windows/PowerShell conventions"
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

- Default workflow reference, not a runtime pipeline.
- Execute the full Jira closed loop by default; trim phases only when the user explicitly says local-only, no-Jira, or requests only one segment.
- Infer the active project first, then read `projects/{project}/knowledge/`; ask only if it cannot be inferred.
- Designing test cases for a Jira card requires Git change analysis — never design from the Jira description alone.
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
2. If knowledge is insufficient, fill the gaps with Git, optional Confluence, the target application, and Kong / OpenAPI context.
3. Read the target Jira card, related issues, and acceptance criteria.
4. Design structured test cases: prefer UI or UI+integration, and retain related APIs, key parameters, scripts, assertions, and evidence requirements in the structured design and generated assets.
5. Write a business-readable test design summary plus the minimum execution contract to a target Jira comment, then read it back immediately to confirm completeness and usability. Do not default to pasting full JSON, every API parameter, long logs, or complete script output into Jira.
6. Create or update a linked `[AI test]` test card based on the confirmed design comment.
7. Generate the Playwright script from the confirmed design comment or test-card scope.
8. Execute the script and collect scenario-level screenshots and recordings. For UI scenarios, drive the page through the same visible entry points a user would use before asserting. Whenever a screenshot carries multiple assertion points, state points, field comparisons, or error messages, default to an annotated screenshot first. Then validate evidence completeness, classify failures, create or update a Bug only for product defects, and complete Jira writeback.
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

- Coverage: generate the Playwright script from confirmed design and persist Jira test assets.
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

- Unless the user explicitly trims the scope, single-issue automation must complete the closed loop `design comment -> test card -> execution -> result writeback -> optional target-issue summary`.
- Evidence must match the test type per scenario. UI scenarios need at least one real product/system UI screenshot and one recording, both renamed per scenario before Jira writeback; unless a screenshot is extremely simple and unambiguous, default to annotated over raw. API-only and file-only scenarios keep request/response or input/output evidence instead and must not fabricate screenshots. Keep applying the existing evidence ownership, naming, attachment, and read-back rules.
- Classify failures before deciding whether to create a Bug. Never automatically treat a script defect, environment timeout, or test-data issue as a product defect.
- Result writeback must land on the test card. A comment on the target requirement card can only be a summary or backlink and cannot replace the complete record on the test card.
