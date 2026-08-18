---
slug: "integration/jira-implementer"
description: "End-to-end Jira-to-PR: read a Jira card, clone/update target repos, implement code changes, commit, push, and open pull requests"
---

# Skill: Jira Implementer

先读 `workflows/tech-lead-jira-implementation-to-pr.workflow.md`（clean-gate/branch/sync 门禁的权威）；此处只保留执行要点与字面约束。

## Triggers

- The user provides a Jira key and asks to implement, develop, or work on the card.
- The orchestration flow enters the `implement-issue` stage.

## Scope

One Jira card at a time; may touch multiple repositories (e.g. frontend + backend). Each repository gets its own feature branch and its own PR.

## Execution Steps

### Step 1: Load Jira Requirements

Read `agents/skills/integration/jira-analyzer.md` and extract: `summary` (one-line description), `acceptanceCriteria` (testable conditions), `apis` (REST/GraphQL endpoints), `affectedModules`, `repositories` (frontend/backend; derive from description, labels, components, or `knowledge/environment`). If repositories cannot be determined, read `knowledge/environment` or ask the user.

### Step 2: Clean Gate, Main Baseline, and Feature Branch

Per the authority workflow, for each repository, in order, before reading or writing implementation files:

1. Clean gate: `git_repo(operation="status", repository="<owner/repo>")` — stop for any uncommitted, staged, untracked, or unexpected change; do not overwrite or clean user changes; ask the user.
2. Branch: `git_repo(operation="branch_prepare", repository="<repo>", branch="feature/{JIRA}")` — fetches latest `main`, creates/reuses the feature branch, auto fast-forward. Non-fast-forward is a blocker; do not rebase or force-update `main`.
3. Branch name exactly `feature/{JIRA}` (e.g. `feature/PROJ-456`); never a user-name branch.

Repos live at `{project_workspace}/repos/<repo-name>/`, reused across tasks; the clean gate must pass again before checkout/pull of an existing repo.

### Step 3: Read Existing Code

Read `agents/skills/integration/git-repo-reader.md` to locate likely-changed files, understand existing patterns (naming, error handling, test structure), and find related tests. Focus on the direct control path.

### Step 4: Implementation Plan (No Confirmation)

After the clean gate, proceed directly with implementation — no `user_confirm` before writing code. Keep track of: **Repositories involved** (repo + role), **Files to modify** (paths relative to each repo root), **Implementation approach** (2–4 sentences), **AC coverage** (which acceptance criteria each change addresses); include them in the final report.

### Step 5: Implement Code Changes on `feature/{JIRA}`

Branch prepared in Step 2 via `git_repo(operation="branch_prepare")`; verify the current branch with `git_repo(operation="status", repository="<repo>")` before writing code (branch format exactly `feature/{JIRA}`, e.g. `feature/PROJ-456`).

Use the `filesystem` tool under `repos/<repo-name>/`. Follow Step 3 patterns: match naming conventions, import style, error handling; do not modify `node_modules/`, `.env`, `*.lock`, or generated files; comment only when the *why* is non-obvious, never restate *what*.

For each file changed, keep a running list: `{repo} → {file path} → {what changed}`.

### Step 6: First Test and Exact-Path Commit

1. Run the first applicable test; record the exact command and result. Database/Redis setup failures may be `environment-blocked` — disclose the dependency and skipped coverage and continue when the test itself cannot run; compilation, assertion, and unknown failures block by default — report them and ask the user; with explicit user confirmation, continue with mandatory disclosure.
2. Stage only the intended files; commit with an exact path list, never a blanket `git add -A`:
   ```json
   git_repo(operation="commit", repository="<repo>", paths=["path/to/file.py", "path/to/test_file.py"], message="<JIRA-KEY>: <concise imperative description>")
   ```
3. Commit without confirmation: stage exactly the intended paths, commit; never commit unrelated user changes.

Commit message: `<JIRA-KEY>: <imperative verb> <what changed>`, e.g. `PROJ-456: add validation on import endpoint`.

### Step 7: Final Synchronization, Push, and Open Pull Request

Before opening a PR, per repository:

1. Sync: `git_repo(operation="sync_branch", repository="<repo>", branch="feature/{JIRA}")` — fetches `origin/main` and `origin/feature/{JIRA}`, merges, reports conflicts explicitly; resolve manually, then re-run.
2. Final test suite, same classification: DB/Redis failures `environment-blocked` (proceed only with disclosure); compilation, assertion, unknown failures block the PR by default — report them to the user and ask for confirmation, then proceed with explicit consent and disclosure.
3. Push: `git_repo(operation="push", repository="<repo>", branch="feature/{JIRA}")` — never `--force`, `--force-with-lease`, or rebase; on rejection, sync_branch again and retry.

Then per repository:

```json
github(
  operation="create_pr",
  repository="<owner/repo>",
  title="<JIRA-KEY> <summary>",
  head_branch="feature/<JIRA-KEY>",
  base_branch="main",
  body="<PR body — see format below>",
  draft=false
)
```

**PR body format:**

```markdown
## Summary
<2–3 sentences describing the change>

## Acceptance Criteria
- [ ] <AC item 1>
- [ ] <AC item 2>
...

## Changed Files
| File | Change |
|---|---|
| `path/to/file.ts` | Added validation logic |

Jira: <JIRA-KEY>
```

### Step 8: Report Results

Summarize: **PRs created** (per repo: number, URL, title, head branch), **Files changed** (repo → file list), **AC coverage** (each AC item → file/function), **Skipped items** (unimplemented AC and why).

## Guardrails

- No `user_confirm` in the implementation flow — neither before writing code nor before the exact-path commit; confirmations are reserved for the dirty-tree blocker, test-failure decisions, and the Technical Clarification Protocol.
- Branch names exactly `feature/{JIRA}`.
- Clean gate must pass before checkout/pull; never overwrite user changes.
- Never force push, force-with-lease push, or rebase `main` or a shared branch.
- Do not approve or merge the PR.
- Never modify `node_modules/`, `.env`, `*.lock`, or auto-generated files.
- Do not push a branch with zero commits.
- Do not call `github(operation="create_pr")` if the push failed.
- Jira card not mappable to a concrete repository → ask the user, don't guess.
- **Never call `git credential-manager` or extract tokens via shell.** Authentication is handled automatically by the `git_repo` and `github` tools; shell token retrieval hangs and times out on Windows.
- **Never write temp files to `/tmp/`** (may not exist on Windows). Use `.tmp/work/` via the `filesystem` tool.

## Error Handling

- Dirty clean gate, non-fast-forward main update, unresolvable conflict → blocks the repository; report.
- Compilation/assertion/unknown test failure → blocks the repository by default; report the failures and ask the user — with explicit user confirmation, delivery may proceed with mandatory disclosure.
- DB/Redis dependency setup failures → `environment-blocked`; continue to PR only after recording the affected test and disclosing the limitation.
- Ordinary push rejected → stop; never force push or rebase to make it pass.
- Push/PR creation returns `403` (`Permission denied` / `Resource not accessible by personal access token`) → the configured Git token lacks write permission. **Do not retry** — report and tell the user: GitHub → Settings → Developer settings → Personal access tokens. Classic token: generate with the `repo` scope checked. Fine-grained token: select the target repository, grant **Contents: Read-only** (Read and write for write operations) plus **Pull requests: Read and write**. The account must be a Write-or-above collaborator. After regenerating, the user updates the token in Settings → Integrations → Git (service key `git`), then retry. Never ask the user to paste the token into chat or knowledge — it lives only in Settings → Integrations.
- `create_pr` returns 422 → the GitHub tool queries open PRs by exact `owner:head` and `base`; return `already_exists` only for one match, otherwise an explicit error.
- A failed step in one repository → continue the others where safe, report clearly at the end.
- Do not approve or merge any PR.
