---
slug: "testing/test-designer"
description: "Design structured, readable test cases by combining project knowledge and Jira cards"
---

# Skill: Test Designer

If `workflows/test-artifact-and-jira-conventions.workflow.md` is not loaded for the current task, read it first.
If the task depends on overall phase order or release-regression context, ensure `workflows/automation-workflow-playbook.workflow.md` is loaded as well.

## Triggers

- The orchestration flow enters the `design-cases` stage.
- Issue-analysis results and project knowledge already exist.

## Design Principles

1. **Coverage dimensions**: main flow -> boundary conditions -> exception paths -> permission scenarios
2. **Automation first**: design only cases that Playwright can execute; skip purely manual checks
3. **UI first**: prefer UI or UI+integration cases via the real user entry path and real clicks over deep-link navigation; fall back to the system's own API only when there is no UI entry, the UI cannot run stably, or the key assertion cannot be covered there.
4. **DB fallback last**: use the self-adapt DB access service only when both UI and the system's own API are unavailable or insufficient.
5. **Reuse knowledge**: reference verified patterns from `test-patterns.md`
6. **Minimum sufficient set**: one clear point per case; do not over-design
7. **Remote-calibrated scope**: absorb the card (issue + comments + transitions) and its linked PRs from the remote (`github` `search_by_jira_key` → `get_pr_detail`) before designing cases; local history supplements only for regression-risk analysis. Do not list cases only from Jira text
8. **Detail placement**: record each identified API, job, service, key parameter, script input, assertion, and evidence requirement in the structured design and generated test assets; do not require the complete execution contract to be copied into Jira prose. When showing results to users or writing them to Jira, lead with the business scenario, outcome, impact, and next action (see Presentation Standard).
9. **Order follows execution priority**: main-flow, happy path, and core regression first; boundary, exception, negative, and corner cases later
10. **Black-box design**: cases target observable business behavior through the UI or the system's own API — never through the product repo's internal unit tests. Do not design "run pytest/vitest/jest on the module" style cases, and never cite unit-test results as evidence.

## Execution Steps

1. Combine inputs: issue-analysis results, project context, and knowledge.
2. For each `testablePoint`, choose the verification path per the Design Principles — UI first (anchored on the live entry path a real user would take), then the system's own API, and only if both are missing or insufficient a self-adapt DB access service step with explicit justification.
3. Design at least one case per `testablePoint`; for `complexity=high` points add extra boundary and exception cases.
4. Classify cases as `smoke`, `regression`, or `edge-case`; fill in the corresponding API, page, job, or data object for each.
5. Order cases with main-flow priority first, negative or corner cases later.
6. Output structured JSON; when presenting externally render it as a table, sectional list, or numbered list.

## Output Structure

```json
{
  "issue": { "key": "QA-123", "summary": "..." },
  "contextSummary": "Brief background based on project knowledge",
  "cases": [
    {
      "id": "QA-123-C01",
      "title": "Main flow - user successfully submits an order",
      "type": "integration",
      "apiOrObject": ["POST /orders", "order service"],
      "objective": "Verify that an order can be submitted on the happy path",
      "preconditions": ["User is logged in", "Shopping cart is not empty"],
      "steps": [
        "Open the order confirmation page /orders/confirm",
        "Fill in the shipping address",
        "Select a payment method",
        "Click the `提交订单` button (Submit Order)"
      ],
      "expectedResults": ["Order success page is displayed", "Order number is generated"],
      "source": ["jira-description", "git:abc123"],
      "tags": ["smoke", "happy-path"],
      "knowledgeRef": ["page-map.md#order-confirm"]
    }
  ]
}
```

## Presentation Standard

For user-facing or Jira-written cases, use a readable Business Summary + Minimal Execution Contract: the structured design and generated test assets remain the source of the complete execution contract; Jira prose does not need to reproduce every parameter or long payload. Per case: Case ID, Type, Scenario, Related Jira issue link, Preconditions, Object/API, Test steps, Expected results, Source, Priority.

Constraints:

- `Object/API` must not be empty; write `Pending confirmation from Git/code` if the interface is not yet confirmed.
- `Source` must include at least one of Jira, Git, or knowledge.
- Backend stories or batch tasks: prefer API, job, or DB-oriented cases over forced UI cases.
- If an acceptance point can be stably observed through the UI, design a UI or UI+integration case first instead of defaulting to pure API.
- UI-ready default step style: `login -> select company/context -> open the visible menu or button -> interact -> assert`. Do not default to direct route opening unless the case explicitly checks direct-route denial or there is no reachable UI entry.
- Unavoidable self-adapt DB calls: wrap the affected Jira-ready step lines in Jira wiki red markup: `{color:red}[SELF-ADAPT-DB] ...{color}`.
- Same-priority default order: main flow or core regression -> boundary conditions -> negative or exception -> corner case.
- If a table becomes hard to read (line wrapping, long steps, JSON code blocks), use a sectional list or numbered list instead.

## Feedback Into Knowledge

Record new UI patterns or testing lessons in the `designInsights` field so `knowledge-manager` can later write them into `test-patterns.md`.
