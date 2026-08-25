---
slug: "integration/git-repo-reader"
description: "Clone, update, and query local git repositories. Records repository references in knowledge. Use when the task requires Git history, code search, Jira-keyed change scope, or historically affected modules."
---

# Skill: Git Repo Reader

## Triggers

- The user provides a git repository URL or path (e.g. `org/repo-name`).
- The task asks to analyze test scope together with historical code changes.
- The task provides a Jira key AND explicitly asks for historical change scope, regression analysis, or historically affected modules.
- Confluence and Jira information are not enough to determine the real change scope.
- The task needs to confirm which modules a feature has historically affected.
- The task needs historical file-level evidence before designing test cases or writing Jira conclusions.

A bare Jira key or PR anchor must NOT trigger this skill: tasks anchored on a Jira card start with
`jira-analyzer` (read the card first), tasks anchored on a PR start with `git-pr-manager` (read the
remote PR first); this skill runs after those, only when historical change scope is still needed.

## Scope Boundary

- This skill handles: repository cloning, pulling, code search, history analysis, and knowledge recording.
- **Read-only analysis**: clone/pull/search/log/show/blame only. Never run the repository's own test suite, build, or lint (pytest, vitest/jest, `npm test`, etc.) — QA verifies the product black-box through UI/API, and the repo's unit-test results are not evidence.
- For live PR queue discovery, author/reviewer filtering, PR diff inventory, review comments, head-SHA checks, approval, or merge handling, use `agents/skills/integration/git-pr-manager.md`.
- If a task starts from a PR anchor, first use `git-pr-manager` to resolve the PR and Jira candidates, then use this skill only when historical change scope is still needed.

## Required Configuration

Git authentication and base URL are handled automatically by the `git_repo`
and `github` tools. The user configures their personal Git token and base URL
in Settings -> Integrations (service key `git`). No environment variables or
knowledge files need to be read for authentication.

The default repository path (e.g. `org/repo-name`) may be stored in project
knowledge `environment/environment` as `GIT_REPOSITORY` for convenience.

## Primary Tool: `git_repo`

All local repository operations use the `git_repo` tool. This tool handles token injection securely and supports extended timeouts for clone/pull.

## Graph First

Clone/checkout/pull results carry a compact code map under `graph`, and the
`repo_graph` tool (query/neighbors/impact/path/report) answers structural
questions about a clone — where a symbol is, what calls it, what depends on
it — in a few tokens. Consult `repo_graph` BEFORE `git_repo search/log/show`
or any file read; read files only for the semantics the graph cannot carry
(it is deterministic, may miss dynamic references, and is a navigation aid,
not a spec).

```json
git_repo(operation="clone", repository="org/repo-name")
git_repo(operation="clone", repository="org/repo-name", branch="develop")
git_repo(operation="checkout", repository="repo-name", branch="main")
git_repo(operation="pull", repository="repo-name")
git_repo(operation="search", repository="repo-name", query="keyword")
git_repo(operation="log", repository="repo-name", path="src/module/")
git_repo(operation="log", repository="repo-name", options="--grep=JIRA-123")
git_repo(operation="show", repository="repo-name", ref="abc123")
git_repo(operation="show", repository="repo-name", ref="HEAD", path="src/file.ts")
git_repo(operation="blame", repository="repo-name", path="src/file.ts")
git_repo(operation="status", repository="repo-name")
git_repo(operation="list_repos")
git_repo(operation="unshallow", repository="repo-name")
git_repo(operation="remove_clone", repository="repo-name")
```

## Secondary Tool: `github`

Use the `github` tool only for operations that require the remote API:

```json
github(operation="search_by_jira_key", jira_key="JIRA-123")
github(operation="get_pr_detail", repository="org/repo", number=456)
github(operation="get_repo_info", repository="org/repo")
```

## Local Repository Management

### Recording a New Repository

When you learn about a git repository URL for the first time:

1. Clone via `git_repo(operation="clone", repository="org/repo-name")` (operations in Primary Tool above). Authentication and base URL resolve automatically from Settings -> Integrations; no URL normalization or knowledge update is needed before cloning.

2. **Do NOT create knowledge files when cloning for learning/exploration purposes.** Only create a knowledge reference file when the repository is a long-term project dependency that other skills will repeatedly reference. Skip this step if the user asked to "look at", "check", "read", or "learn" the code; the clone is temporary or exploratory (e.g. analyzing a single PR or file) — if the user later asks to clean it up, delete it via `remove_clone` (see "Removing a Temporary Clone"); or the repository was already recorded in knowledge from a prior session.

   If you do need to persist the reference (e.g. the user explicitly says "add this repo to the project"), create:
   ```json
   knowledge_rw(operation="write", slug="technical/repo-{name}", content="---\ncategory: technical\nslug: technical/repo-{name}\ntags: [repository, codebase]\ntitle: \"Repository: {name}\"\nparent: technical/technical-stack\nsummary: \"{brief description}\"\n---\n\n# Repository: {name}\n\n## Overview\n- **URL**: {git_url}\n- **Default Branch**: {branch}\n- **Clone Path**: `repos/{name}`\n- **Last Pulled**: {date}\n- **Clone Type**: shallow (depth=1)\n\n## Key Directories\n{analyze with git_repo search/list}\n\n## Related Knowledge\n- [[environment/environment]]\n- [[technical/technical-stack]]\n", change_summary="Record new repository reference")
   ```

### Updating an Existing Repository

Before querying history, ensure the local clone is current and on the default branch:

1. Check if the repo exists: `git_repo(operation="list_repos")`.
2. If it exists, switch to `main` and pull latest (operations in Primary Tool above) so local evidence matches the latest remote state. Checkout and pull require a clean working tree; if either reports a dirty-tree blocker, report it and do not reset, stash, or overwrite user changes.
3. If it does not exist but the URL is known from knowledge, clone it (Primary Tool above).

### When Full History Is Needed

Shallow clones cannot run full-history `git log` or `git blame` on old commits. If a history query returns incomplete results, run `git_repo(operation="unshallow", repository="repo-name")`.

### Removing a Temporary Clone

When the user asks to clean up or delete a clone (e.g. "删除克隆" / "清理仓库"), or a clone created only for exploratory analysis is no longer needed, remove it with:

```json
git_repo(operation="remove_clone", repository="repo-name")
```

This is the ONLY supported way to delete a clone: it asks the user to confirm before deleting, and removes both the checkout under `repos/{name}` and its auto-built code-graph cache under `.tmp/repo_graph/{name}`. Deleting is permanent — local changes, unpushed commits, and the code graph are destroyed — so call it only when the user has explicitly asked for cleanup, and surface the confirmation prompt when it appears. Never attempt `rm -rf` via the shell tool: it is not allowed and would not remove the code-graph cache.

## Data Source Priority

1. **Authoritative task source first** — the Jira card via `jira` (`jira-analyzer`) for card tasks, or the live remote PR via `github` (`git-pr-manager`) for PR tasks.
2. **Enterprise Git platform via `github`** — search commits / PR references / changed files by Jira key (`search_by_jira_key`); then `get_pr_detail` on linked PRs for diff, reviews, comments, and checks.
3. **Local repository via `git_repo` (supplement)** — code search, Jira-keyed commit search, file history, commit details, line-level attribution, when the local repository has historical branches or cross-repository history the remote search cannot provide.

## Execution Steps

### Step 1: Ensure Local Repository Available

Run this step ONLY when the task needs historical change scope AND the authoritative source
(Jira card via `jira`, remote PR via `github`) has already been read. Never `list_repos`/clone/pull
before reading the Jira card or the remote PR.

Check `git_repo(operation="list_repos")`; clone if the target repo is missing, pull if it exists (operations in Primary Tool above). The tool resolves the Git host and credentials automatically from Settings -> Integrations.

### Step 2: Determine Search Keys

Construct search criteria in this order:

1. Jira key, for example `PROJ-123`
2. Module keywords from the Jira summary
3. Page names, interface names, or service names explicitly mentioned by the user
4. Domain terms from the related glossary

### Step 3: Search Historical Changes

Collect at least the following information:

- Related commit list: `git_repo(operation="log", options="--grep=JIRA-KEY")`
- File paths changed and keyword code search: `git_repo` `show` / `search` operations (Primary Tool above)
- Related PR references: `github(operation="search_by_jira_key", jira_key="KEY")` — call this BEFORE local `--grep`; local `log --grep` covers commits the remote search may not index
- Modules, services, or pages touched by the changes
- Business terms or risk hints mentioned in commit messages

If the task originated from a PR, reuse the Jira key, file list, and changed modules already resolved by `git-pr-manager` instead of rediscovering the live PR inventory here.

### Step 4: Derive Testing Impact Scope

Derive the following from historical changes:

- directly changed modules
- indirectly affected modules
- common interaction points
- regression points that are easy to miss
- whether existing automation likely already covers them
- APIs, batch endpoints, or internal jobs that must be operated or verified

Pay special attention to: page entry points or routes; form and table components; API clients or connectors; service layer or orchestration code; configuration files and feature toggles; permission decision logic; templates, export flows, and reporting logic; test-helper interfaces or mock configuration.

### Step 5: Output Historical Insights

The output must include at least:

```json
{
  "jiraKey": "PROJ-123",
  "repository": "repo-name",
  "relatedCommits": ["abc123", "def456"],
  "relatedPullRequests": [1234],
  "apis": [
    {
      "name": "POST /orders/import",
      "layer": "controller",
      "evidence": "commit abc123"
    }
  ],
  "changedAreas": [
    "order list page",
    "order service",
    "document template controller"
  ],
  "regressionRisks": [
    "export/report affected",
    "<variant-a>/<variant-b> branch logic affected"
  ],
  "testSuggestions": [
    "verify list filtering",
    "verify status transition",
    "verify template download"
  ]
}
```

If the user's goal is to design test cases or write a Jira comment, the Git analysis must at least add these two kinds of information:

1. Which commits or related PR references prove the real change scope.
2. Which APIs, jobs, services, or controllers must be directly operated or specially validated during testing.

## Integration With Test Design

Git analysis is not a standalone deliverable; it feeds directly into the test-design stage. Pass at least the following to `test-designer`:

- modules that must be covered by regression
- historically high-frequency change points
- files indicating permission, configuration, or export-related risks
- cases suitable for API testing vs those requiring UI-linked validation
- involved APIs and how to fill the `Object/API` column in the test-case table

When the task later proceeds to PR review, pass historical findings to `pr-review-manager` only as supporting evidence. Live PR inventory, comments, checks, approval, and merge handling come from `git-pr-manager`.

## Requirements For Updating Knowledge

**Default: do NOT write knowledge.** Only update when ALL are true:
1. The task explicitly requires persisting findings (e.g. "record this", "update knowledge", or a downstream skill like test-designer needs it).
2. The conclusions are stable and reusable across future sessions.
3. The repository is a long-term project dependency, not a one-off exploration.

If the user only asked to clone and read/learn the code, finish without creating or updating any knowledge files.

When all conditions above are met and Git analysis yields stable conclusions, write them into knowledge:

- `context.md`: newly discovered module boundaries and system relationships
- `page-map.md`: newly confirmed page entry points and function mappings
- `test-patterns.md`: newly confirmed high-risk regression patterns
- `history.md`: the conclusions learned from Git during this run
- `technical/repo-{name}.md`: update "Key Directories" section and "Last Pulled" timestamp

Recommended `history` format:

```markdown
- 2026-05-15 | git:PROJ-123 | Extracted historical change risks for order list, export, and permission check
```

## Error Handling

- If the git token is missing, return a clear message asking the user to add it in Settings → Integrations (service key: `git`).
- If Git returns `403` (`Permission denied` / `Resource not accessible by personal access token`), the configured Git token lacks access to the repository. **Do not retry** — report this and tell the user how to fix it: GitHub → Settings → Developer settings → Personal access tokens. For a classic token, generate one with the `repo` scope checked. For a fine-grained token, select the target repository and grant at least **Contents: Read-only** (write operations need Read and write). The account must also be a collaborator on the repository. After regenerating, the user updates the token in Settings → Integrations → Git (service key `git`) and then the flow can be retried. Never ask the user to paste the token into chat or knowledge — it lives only in Settings → Integrations.
- If clone times out, suggest the user check network connectivity or try a specific branch.
- If the local repository has no matching records, continue searching the enterprise Git platform via `github` tool.
- If there are too many search results, keep the most recent, most relevant, and most file-concentrated records first.
- If the Jira key cannot be found via git grep, fall back to `--grep` in git log, then to the `github` API.
- If shallow history is insufficient, use `git_repo(operation="unshallow")` before retrying.
