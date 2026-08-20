---
slug: "integration/task-source-priority"
description: "Task-type → authoritative-source priority table. Read first for any task referencing a ticket (Jira key), pull request, or issue anchor: call the mapped integration tool directly as the first action — no local repository preamble."
routing:
  - id: "jira-card"
    priority: 10
    label: "a Jira card"
    anchors:
      - "(?<![A-Za-z0-9])[A-Z]{2,}-\\d+(?![A-Za-z0-9])"
    tool: "jira"
    first_ops: ["get_issue", "get_comments", "get_transitions"]
    follow_ups:
      - tool: "github"
        op: "search_by_jira_key"
        note: "to find the card's PRs, then get_pr_detail on each — diff, reviews, comments, checks"
  - id: "pull-request"
    priority: 20
    label: "a pull request"
    anchors:
      - "(?<![A-Za-z0-9])pull\\s+request(?![A-Za-z0-9])"
      - "(?<![A-Za-z0-9])pr(?![A-Za-z0-9])"
      - "/pull/\\d+"
      - "合并请求"
    tool: "github"
    first_ops: ["get_pr_detail", "list_prs"]
    follow_ups:
      - tool: "jira"
        op: "jira-analyzer"
        note: "on the Jira key found in the PR title, branch, or commits"
---

# Skill: Task Source Priority

This table is the single source of truth for **where a task's authoritative context comes from**.
It is consumed by two layers: the agent prompt (this body) and the per-turn routing directive
(the `routing:` frontmatter, injected by the framework when the user message contains an anchor).

## Priority Table

| Anchor in the user message | First action (authoritative source) | Follow-ups | Local repo role |
|---|---|---|---|
| Jira key (e.g. `QA-123`) | `jira` `get_issue` → `get_comments` → `get_transitions` — the card, its comments, and its transitions are the requirement's authority | `github` `search_by_jira_key` to find the card's PRs, then `get_pr_detail` on each — diff, reviews, comments, checks | Supplement only: historical change scope / regression risk the remote cannot provide |
| PR anchor (PR URL, `/pull/<N>`, `review this PR`, `合并请求`, `#N` with a PR context) | `github` `get_pr_detail` (or `list_prs` for a queue) — the remote diff, reviews, comments, and checks are authoritative | `jira-analyzer` on the Jira key found in the PR title, branch, or commits | Supplement only; never the first action |
| Implementation task (branch / commit / push / test on a card) | Local `git_repo`: dirty-tree `status` → fetch/pull → `feature/{JIRA}` branch | `github` `create_pr` after push | **Primary** — local state genuinely matters; keep the existing gates |

## Universal Rule

When a task references a Jira key or a PR anchor, **call the mapped tool directly as the first
tool action**. Do not fumble: no `git_repo(operation="list_repos"/"status"/"pull")` exploratory
preamble before the authoritative call. The local checkout answers only what the remote cannot —
historical change scope, regression risk, or implementation-local state.

## Adding a New Platform

A new integration (e.g. GitLab issues) needs **no code change**:

1. Add a `routing:` entry above: `id`, `priority` (higher wins when several anchors match),
   `label`, `anchors` (regex list, ASCII-alphanumeric boundaries recommended so CJK-adjacent
   anchors still match), `tool` (must be declared in the agent's frontmatter `tools:`), `first_ops`,
   optional `follow_ups` (`tool` + `op` + `note`).
2. Declare the tool in the agent definition frontmatter (`tools:`).
3. The per-turn directive is generated from the entry automatically.

Do NOT add `triggers:` to this skill — trigger matching scans the global registry unfiltered and
would inject this table into unrelated agents' turns.
