---
slug: "testing/release-regression-manager"
description: "Generate release-level test cards from Jira release links, and design both card regression and main-flow regression"
---

# Skill: Release Regression Manager

If `workflows/test-artifact-and-jira-conventions.workflow.md` is not loaded for the current task, read it first.
Ensure `workflows/automation-workflow-playbook.workflow.md` has been loaded — the default release-regression flow, output structure, and Bug linking rules follow that common document; this skill only adds release design and card-landing details.

## Trigger Conditions

- The user provides a Jira release/version link.
- The user asks to `按 release 做回归测试设计`, meaning to design regression testing by release.
- The user asks to `创建一张 release 级测试卡`, meaning to create a release-level test card.

## Goal

Based on a Jira release link, produce one release-level summary test card and one corresponding test cycle, and split the regression design into two layers:

1. Card regression: regression scenarios and steps for each card in the release.
2. Main-flow regression: business main flows spanning cards, modules, or interfaces.

## Execution Steps

1. Call `jira(operation="get_release_scope", version_id="<versionId>")` to read the release metadata and the issue list; preserve the release link, version id, JQL, and total issue count. Note: `version_id` and `cycle_project_id` are Jira **numeric IDs**, not names — the Jira tool has no discovery operation to look them up by name, so ask the user for the numeric IDs when they are not already known.
2. Classify each issue as directly designable, requiring more Git/code confirmation, or currently untestable.
3. Create the release-level summary test card. Suggested title: `[AI test][Release <PROJECT>-<VERSION-ID>] <release name> regression summary`.
4. Create the corresponding test cycle. Suggested name: `[AI test][Cycle Release <PROJECT>-<VERSION-ID>] <release name> SIT`.
5. The summary test card description must include at least: Release Link, Release Metadata, Release Test Cycle, Release Scope Summary, Card Regression Coverage, Untestable Cards, Main Flow Regression, Evidence And Recording Rules, Bug Linking Rules.
6. For each testable issue, generate one cycle-ready card-regression entry retaining at least: issue key, issue link, test scenario, test steps, execution steps, involved API/page/object, and expected result. The complete execution detail lives in the test cycle or structured artifact; Jira summary prose may point to it.
7. Write `测试步骤` and `执行步骤` separately, SIT-style: `测试步骤` (test steps) emphasizes prerequisites, inputs, actions, and checkpoints; `执行步骤` (execution steps) emphasizes the concrete environment, account, data, evidence ownership, and result-recording method for the current run.
8. Untestable issues go in the `Untestable Cards` section with a clear reason, e.g. no usable entry point, environment data cannot be created, a dependency is not deployed, missing permission, or insufficient requirement information.
9. Main-flow regression must not restate the card list; design cross-card scenarios and steps such as primary status transitions, document/notification chains, approval chains, and external-system interactions.
10. During execution, UI scenarios record real system video by default, with key screenshots preferably annotated; API-only and file-only scenarios keep their applicable non-visual evidence (request/response or input/output files) and must not create video or screenshot requirements unless a system UI is actually involved. For UI scenarios, video, raw screenshots, and annotated screenshots are assigned clearly in both the test steps and the execution steps.
11. Bug linking: card-regression Bugs link to both the current release test card and the corresponding original Jira card; main-flow-regression Bugs link only to the current release test card — never auto-link to an uncertain original card.
12. The execution-result comment records a concise business status and outcome per scenario, business impact, Bug status, next action, and scenario-owned evidence references; the full step-by-step contract, assertions, and long logs stay in the test cycle or execution artifacts.

## Output Constraints

- The release-level summary test card is a summary card; the test cycle is the execution container. By default, do not create a separate Jira test card per release issue unless the user explicitly asks.
- Each testable card gets at least one cycle-ready entry that includes both `测试步骤` and `执行步骤`.
- `Card Regression Coverage` covers all testable cards; `Untestable Cards` covers all untestable ones; together they equal the release issue list.
- Summary card description stays stable and concise: release scope, business regression scenarios, expected outcomes, coverage, and links to the test cycle and assets; the test cycle carries the complete per-card execution entries. Comments use Business Summary + Minimal Execution Contract: status, business result, impact, evidence, Bug, next action — not full technical execution output.
- Every card-regression entry includes the original Jira card link; main-flow regression includes at least the release test card link and the release link.
- Video, screenshot, and annotated-image names reflect the release, case id, and scenario meaning, mapping one-to-one to the test steps.

## Recommended Tool Calls

```json
jira(operation="get_release_scope", version_id="<versionId>")
jira(operation="create_issue", project_key="<PROJECT-KEY>", summary="[AI test][Release <PROJECT>-<VERSION-ID>] <release name> regression summary", issue_type="Test", labels=["AITest", "release-regression"], description="<content>")
jira(operation="create_test_cycle", cycle_name="[AI test][Cycle Release <PROJECT>-<VERSION-ID>] <release name> SIT", cycle_project_id="<projectId>", version_id="<versionId>", description="<content>")
jira(operation="add_comment", issue_key="<TEST-KEY>", comment_body="<execution result>")
jira(operation="add_attachment", issue_key="<TEST-KEY>", file_path="tests/generated/artifacts/<release>/<case>-<scenario>.webm")
```
