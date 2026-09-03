---
slug: "integration/git-pr-manager"
description: "Discover, inspect, approve, and merge GitHub Enterprise pull requests via the github tool. Use when the task asks to list PRs, inspect a PR diff, find PRs for a person or reviewer, check open PR queues, approve or merge a PR, or gather PR evidence before review, approve, or merge."
---

# Skill: Git PR Manager

## Triggers

- The user asks to query PRs, inspect open PR queues, or find PRs for a person or branch.
- The user asks for the current user's open / opening PRs, meaning open PRs created by other users that request review from the current authenticated user across repositories.
- The user asks to review a PR, confirm PR change scope, or gather evidence before approve / merge.
- The user asks to approve PRs in batch or individually.
- The user asks to merge already approved PRs.
- The user asks to add a comment to a PR (issue comment / general comment on the PR conversation).
- The current task needs PR title, author, reviewers, checks, changed files, commits, review comments, issue comments, approval status, or mergeability from GitHub Enterprise.
- The task needs to disambiguate author login, reviewer login, repository, or branch before a PR operation.

## Scope Boundary

- This skill is for live PR discovery, PR detail retrieval, approval, and merge handling.
- For Jira-linked historical commits, historically affected modules, and regression patterns across time, use `agents/skills/integration/git-repo-reader.md`.
- `pr-review-manager` still owns the review conclusion itself. This skill supplies the PR evidence and executes approve / merge only when the user explicitly asks.
- This skill is intended for `tech-lead` workflows. Do not attach it to `quality-assurance`; that agent should continue using only `git-repo-reader` for Git read-only history and Jira-linked scope discovery.
- **Context-only, read-only governance**: PR inspection and evidence gathering never execute the PR's own test suite, build, or lint (pytest, vitest/jest, `npm test`, etc.). All PR judgments come from remote context (`get_pr_detail`/`get_commit_checks`) and local read-only inspection (`git_repo` show/search/log, `repo_graph`). The remote check results are the only valid execution evidence for a PR.

## Required Configuration

Git authentication and base URL are handled automatically by the `github` and
`git_repo` tools. The user configures their personal Git token and base URL in
Settings → Integrations (service key `git`). No environment variables or
knowledge files need to be read for authentication.

The default repository path (e.g. `org/repo-name`) may be stored in project
knowledge `environment/environment` as `GIT_REPOSITORY` for convenience, but
the `repository` parameter can also be passed directly to each tool call.

## Tool Calls

Use the `github` tool for all PR operations. Authentication is handled automatically from user tokens.

```json
github(operation="get_repo_info", repository="<owner/repo>")
github(operation="get_user")
github(operation="list_prs", repository="<owner/repo>", state="open")
github(operation="list_prs", reviewer="<login>", state="open", all_repos=true)
github(operation="list_prs", repository="<owner/repo>", state="open", author="<login>")
github(operation="list_prs", repository="<owner/repo>", state="all", base="<branch>", head="<branch>")
github(operation="get_pr_detail", repository="<owner/repo>", number=<N>)
github(operation="get_pr_detail", url="<PR-URL>")
github(operation="approve_pr", repository="<owner/repo>", number=<N>, body="<approval comment>")
github(operation="approve_pr", url="<PR-URL>")
github(operation="merge_pr", repository="<owner/repo>", number=<N>, merge_method="squash")
github(operation="merge_pr", url="<PR-URL>", merge_method="rebase", sha="<HEAD-SHA>", commit_title="<title>")
github(operation="add_pr_comment", repository="<owner/repo>", number=<N>, body="<comment text>")
github(operation="add_pr_comment", url="<PR-URL>", body="<comment text>")
github(operation="fork", repository="<owner/repo>")
github(operation="is_starred", repository="<owner/repo>")
github(operation="star", repository="<owner/repo>")
```

Tool behavior:

- `get_repo_info` returns `owner/repo`, default branch, and description.
- `get_user` resolves the current authenticated GitHub Enterprise login.
- `list_prs` reads the repository PR queue and supports `state`, `author`, `reviewer`, `base`, `head`, and `limit` filters. With `all_repos=true`, it switches to cross-repository search.
- `get_pr_detail` reads one PR and returns metadata, changed files, commits, reviews, and head-SHA check status.
- `approve_pr` submits an `APPROVE` review with an optional body comment.
- `merge_pr` merges a PR with `merge`, `squash`, or `rebase`, and can pin the expected head SHA.
- `add_pr_comment` posts an issue comment (visible in the PR conversation timeline) — for review feedback, status updates, or summaries without submitting a formal review. Requires `body`.

## Data Source Priority

1. **`github` tool for PR content (authoritative, first)**
  - The remote PR is the authority for PR work: PR inventory, diff, commits, reviews, comments, checks, approval, and merge all live on the remote platform — use the `github` operations in Tool Calls above as the FIRST action for any PR-anchored task.
  - Call `approve_pr` only after the user explicitly requests approval; call `merge_pr` only after the user explicitly requests merge.
  - Never use `shell(command="git remote get-url origin")` — the clone URL contains an embedded token and will leak credentials in shell output.

2. **`git_repo` tool for local repository state (supplement, only when needed)**
  - Use the local checkout only: (a) to discover a repository anchor from the workspace when the repo is not derivable from the PR URL / owner+repo; (b) for implementation tasks needing the dirty-tree gate; (c) for historical evidence the remote cannot provide (`show`/`search`/`log`).
  - Any uncommitted, staged, or untracked change in a target repo is relevant scope context that must not be overwritten.
  - Local inspection is read-only for PR work: `show`/`search`/`log`/`blame` and `repo_graph` only. Never run the repository's own test suite to validate a PR.

3. **Enterprise Git platform as a supplement**
  - When the local repository does not match the target repository, use the enterprise Git API with explicit repository anchors.
  - When a natural-language person name is ambiguous, derive candidates from live PR authors and reviewers before choosing one target.

## Execution Steps

### Step 1: Determine PR Anchors

Construct search criteria in this order:

1. PR URL
2. owner/repo + PR number
3. current repository + PR number
4. author login, reviewer login, or branch name
5. Jira key reverse-located from PR title, body, branch, or commits

### Step 2: Discover PR Inventory

When the task is to query PRs, review repository PRs, or find PRs for a person, collect at least:

- repository owner and name
- open PR list in the current repository
- PR number, title, author, state, draft flag, base, head, and URL
- requested reviewers when the task is reviewer-oriented
- candidate matches for ambiguous natural-language names such as `fan` or `qiang`

Use the `github` tool (operation forms in Tool Calls above):

1. When the task is anchored on a PR or a PR queue, call `github` directly (`get_pr_detail`/`list_prs`) — do not precede remote calls with `git_repo` `list_repos`/`status`/`checkout`/`pull`. The local-clone preamble applies only to implementation tasks (dirty-tree gate, pull-latest before coding) or to discovering a repository anchor the remote cannot resolve.
2. `github(operation="get_repo_info", repository="<owner/repo>")` to resolve the current repository when the repo is not explicitly given.
3. `github(operation="list_prs", repository="<owner/repo>", state="open")` to read the repository queue.
4. `github(operation="get_user")` when the task is reviewer-oriented and the current login must be resolved.
5. `github(operation="list_prs", reviewer="<login>", state="open", all_repos=true)` for the current user's cross-repository review queue — whether the user asks for PRs needing their review or for their own open / opening PRs.
6. `github(operation="list_prs", repository="<owner/repo>", state="open", reviewer="<login>")` or `github(operation="list_prs", repository="<owner/repo>", state="open", author="<login>")` when the scope is explicitly one repository.

Queue wording rule: `PRs that need my review`, `review-requested`, `my open/opening PRs`, `current user's opening PRs`, `当前用户下的所有 opening PR`, or equivalent reviewer wording means the reviewer queue. Only use the author queue when the user explicitly says PRs created/authored by the current user.

If the task uses a natural-language person name rather than a login, first derive candidates from the repository's open PR authors and requested reviewers before choosing a target list.

### Step 3: Read Single-PR Detail

When the task is anchored to one PR, collect at least:

- PR metadata: title, author, base, head, state, draft, merge commit SHA when available
- PR file list and patch summary
- commit list in the PR
- existing review comments and review states when available
- issue comments when available
- head SHA for checks and merge-decision follow-up

Use the `github` tool:

1. `github(operation="get_pr_detail", repository="<owner/repo>", number=<N>)` when the repository is explicit.
2. `github(operation="get_pr_detail", url="<PR-URL>")` when the PR URL is the only stable anchor.

### Step 4: Pre-Approval Checks

Before approval, confirm at least:

- the review target list is unambiguous
- the user explicitly requested approval
- `pr-review-manager` concluded `approve-safe`
- the PR is still open and not draft
- the current account has not already submitted the latest `APPROVED` review unless re-approval was explicitly requested

Use an English approval comment that reflects the actual review context.

### Step 5: Submit Approval

When the prechecks pass, call `approve_pr` with either repository+number or URL form (see Tool Calls above). Record at least:

- repo
- PR number
- review state
- review URL

### Step 6: Pre-Merge Checks

Before merge, confirm at least:

- the user explicitly requested merge
- `state == open`
- `draft == false`
- `mergeable == true` when the detail endpoint exposes it (may be `null` while the mergeability check is still pending)
- repository permissions and strategy constraints have been checked when merge permission is uncertain

Use the PR detail result as the main pre-merge source, then select `merge`, `squash`, or `rebase` according to the user's request or repository policy.

### Step 7: Merge And Validate

When the prechecks pass, call `merge_pr` with the `merge_method` per the user's request or repository policy (see Tool Calls above). After merge, retain at least:

- repo
- PR number
- whether `merged` is true
- merge SHA
- merge response message

### Step 8: Output PR Evidence And Results

The output must include at least:

```json
{
  "repository": "your-org/your-repo",
  "pullRequest": {
    "number": 1234,
    "title": "PROJ-123 fix import validation",
    "author": "user",
    "state": "open",
    "draft": false,
    "base": "main",
    "head": "user/proj-123",
    "headSha": "abc123"
  },
  "changedFiles": ["src/connectors/example.ts"],
  "commits": ["abc123", "def456"],
  "reviewCommentsRead": true,
  "checksAnchorSha": "abc123",
  "jiraKeyCandidates": ["PROJ-123"],
  "approval": {
    "state": "APPROVED"
  },
  "merge": {
    "merged": true,
    "mergeSha": "ca3ce05203499478e0390343c7d81417b5285533"
  }
}
```

If the user's goal is to review, approve, or merge a PR, the PR analysis must also add these three kinds of information:

1. Which PRs matched the requested person, repo, branch, or Jira key, and why.
2. Which files, control points, and commits must be inspected before review conclusions are made.
3. Which evidence and action results should be handed to `pr-review-manager`, including PR number, head SHA, changed files, review comments, approval result, merge result, and Jira key candidates.

## Integration With PR Review

When the task is PR review, pass at least the following to `pr-review-manager`:

- normalized PR target list
- resolved repository owner/name and PR numbers
- head SHA and changed-file inventory
- review comments and issue comments when available
- Jira key candidates and the evidence source for each candidate
- direct control-path files that should be reviewed before approval or merge

If the PR alone does not show enough historical scope, then call `git-repo-reader` next for Jira-linked history and regression patterns.

## Guardrails

- Approval and merge are remote mutation actions. Execute them only when the user explicitly requests them.
- Do not approve by default when the review conclusion is `approve-with-risks` or `do-not-approve`.
- Do not merge by default after approval; merge still requires an explicit user request.
- Never echo tokens, passwords, or authentication headers.
- All remote calls must keep explicit timeouts.
- PR evidence is context-only: never execute the repository's test suite, build, or lint to validate a PR. Check status (`get_commit_checks`) is the only execution evidence used.

## Error Handling

- If the current repository has no open PRs, explicitly report that the current remote repository has no open PRs.
- If the enterprise Git token is missing, preserve any locally derived repository, branch, or PR anchors and state clearly that remote-platform PR information is unavailable.
- If multiple author candidates match a natural-language name, return the candidate list and the matching PRs instead of picking one silently.
- If the task anchor is ambiguous, return the candidate repositories or PRs before proceeding to any review, approval, or merge-stage decision.
- If merge returns `404`, inspect repository permissions first before assuming the PR does not exist.
- If merge returns `405` or `409`, reread PR detail and report branch protection, merge-strategy restrictions, conflicts, or head-SHA drift explicitly.
- If Git returns `403` (`Permission denied` / `Resource not accessible by personal access token`), the configured Git token lacks permission for the target repository. **Do not retry the direct path** — when the task is to *contribute* code (create a PR), fall back to the fork flow per `workflows/tech-lead-jira-implementation-to-pr.workflow.md` section 5b: `github fork` → push with `git_repo(push, target_repository="<account>/<repo>")` → `create_pr` with `head_branch="<account>:<branch>"` → star courtesy (ask the user first via `user_confirm`). When the task is only to *read or review*, report the 403 and tell the user how to fix it: GitHub → Settings → Developer settings → Personal access tokens. For a classic token, generate one with the `repo` scope checked. For a fine-grained token, select the target repository and grant **Contents: Read and write** plus **Pull requests: Read and write**. The account must also be a Write-or-above collaborator on the repository (for org-owned repos, check the org role too). After regenerating, the user updates the token in Settings → Integrations → Git (service key `git`) and then the flow can be retried. Never ask the user to paste the token into chat or knowledge — it lives only in Settings → Integrations.
