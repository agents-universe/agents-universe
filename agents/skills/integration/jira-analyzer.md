---
slug: "integration/jira-analyzer"
description: "Parse Jira card content and extract acceptance criteria and testable points"
---

# Skill: Jira Analyzer

## Triggers

- The user provides the `--issue` parameter.
- The orchestration flow enters the `load-issue` stage.

## Priority

First action for any task referencing a Jira key — the card is the requirement's authority:

1. `jira(operation="get_issue_context", issue_key="<JIRA-KEY>")` — the card, its comments, and its transitions in one call
2. `github(operation="get_pr_details", jira_key="<JIRA-KEY>")` — every linked PR in one call

This reads the same card and the same PRs as before; it just does it in two round trips instead of
one per item. Do not escalate to the one-at-a-time calls (`get_issue`, `get_comments`,
`get_transitions`, `get_pr_detail`) unless the batch reports that a section failed — a failing
section reports itself in place (`comments_error`, `transitions_error`, or one PR's `error`) and
never invalidates the rest of the payload.

The local `git_repo` checkout is a supplement only, for historical scope the remote cannot provide.
Never precede these steps with `git_repo(operation="list_repos"/"status"/"pull")` exploratory calls.

## Execution Steps

Use the `jira` tool for all Jira API calls. Authentication is handled automatically from user tokens.

**Tool calls:**
```json
jira(operation="get_issue_context", issue_key="<JIRA-KEY>")
github(operation="get_pr_details", jira_key="<JIRA-KEY>")
```

1. `jira(operation="get_issue_context", issue_key="<JIRA-KEY>")` — parse the description for feature description, acceptance criteria, attached business rules, and related UI elements. It also carries comments and transitions, so one call covers steps 2-3 below.
2. Use the card's comments when existing test-design comments, execution comments, or prior AI notes matter: read the returned `comments`, and use the latest structurally complete one as evidence. Only if `comments_error` is present, retry once with `jira(operation="get_comments", issue_key="<JIRA-KEY>")`.
3. Use the returned `transitions` when status or allowed transitions matter. Only if `transitions_error` is present, retry once with `jira(operation="get_transitions", issue_key="<JIRA-KEY>")`.
4. `github(operation="get_pr_details", jira_key="<JIRA-KEY>")` — the card's linked PRs with diff, reviews, comments, and checks. This replaces one `get_pr_detail` call per PR; fall back to `get_pr_detail` only for a single PR the batch could not resolve.
5. Infer test tags from labels and components.
6. Cross-reference knowledge: glossary domain terms, `page-map` pages, `test-patterns` patterns. Knowledge files that are not `knowledge_level: detail` are already in your context — cite them directly instead of re-reading them with `knowledge_rw`.
7. For test-case design or Jira comments, also output: Git commit/PR clues to inspect; APIs / batch jobs / downstream systems known or inferable from the description; interface and module boundaries still requiring Git analysis.

## Output Structure

```json
{
  "key": "QA-123",
  "summary": "...",
  "apis": [
    {
      "name": "POST /api/orders",
      "source": "jira-description",
    "purpose": "Create an order"
    }
  ],
  "gitInvestigationHints": [
    "search commit by QA-123",
    "inspect order controller and service"
  ],
  "testablePoints": [
    {
    "point": "Form submission succeeds",
      "source": "AC-1",
      "relatedKnowledge": ["page-map.md#submit-form"]
    }
  ],
  "suggestedTags": ["smoke", "form"],
  "relatedPages": ["/orders/create"],
  "complexity": "medium"
}
```

## Complexity Assessment

- **low**: single page, no external dependencies, acceptance criteria count <= 3
- **medium**: cross-page behavior or data dependencies
- **high**: third-party integration, concurrency scenarios, or complex permissions

## Relationship To Knowledge

Terms or page paths not in knowledge yet → mark as `knowledge-gap` so `knowledge-manager` can decide whether to extend the knowledge base.

## Output Constraints

- Jira text alone insufficient for final test cases → state clearly that Git analysis must continue. Git analysis means the card's linked PRs on the remote first (`get_pr_details` with the card's `jira_key`), local history second.
- Jira does not explicitly mention APIs → still list APIs pending Git/code confirmation instead of omitting that field.
- When handing results to `test-designer`, prioritize table-friendly fields: module, API, risk point, source.
