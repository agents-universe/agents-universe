---
slug: "integration/jira-analyzer"
description: "Parse Jira card content and extract acceptance criteria and testable points"
---

# Skill: Jira Analyzer

## Triggers

- The user provides the `--issue` parameter.
- The orchestration flow enters the `load-issue` stage.

## Execution Steps

Use the `jira` tool for all Jira API calls. Authentication is handled automatically from user tokens.

**Tool calls:**
```json
jira(operation="get_issue", issue_key="<JIRA-KEY>")
jira(operation="get_comments", issue_key="<JIRA-KEY>")
jira(operation="get_transitions", issue_key="<JIRA-KEY>")
```

1. `jira(operation="get_issue", issue_key="<JIRA-KEY>")` — parse the description for feature description, acceptance criteria, attached business rules, and related UI elements.
2. When existing test-design comments, execution comments, or prior AI notes matter: `jira(operation="get_comments", issue_key="<JIRA-KEY>")`; use the latest structurally complete comment as evidence.
3. When status or allowed transition matters: `jira(operation="get_transitions", issue_key="<JIRA-KEY>")`.
4. Infer test tags from labels and components.
5. Cross-reference knowledge: glossary domain terms, `page-map` pages, `test-patterns` patterns.
6. For test-case design or Jira comments, also output: Git commit/PR clues to inspect; APIs / batch jobs / downstream systems known or inferable from the description; interface and module boundaries still requiring Git analysis.

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

- Jira text alone insufficient for final test cases → state clearly that Git analysis must continue.
- Jira does not explicitly mention APIs → still list APIs pending Git/code confirmation instead of omitting that field.
- When handing results to `test-designer`, prioritize table-friendly fields: module, API, risk point, source.
