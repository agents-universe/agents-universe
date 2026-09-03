---
slug: "integration/pr-review-manager"
description: "Review PR checks, code defects, and Jira requirement satisfaction, and output findings plus a risk conclusion"
---

# Skill: PR Review Manager

## Trigger Conditions

- The user asks to `review` a PR.
- The user asks to `review 仓库里的 PR`, meaning to review the PRs in the repository.
- The user asks to see all open PRs in the current repository.
- The user asks to see which PRs currently need their review.
- The user asks to see the current user's open / opening PRs, meaning open PRs created by other users that request review from the current authenticated user across repositories.
- The user asks to confirm whether `all checks have passed`.
- The user asks to determine whether the implementation has bugs / regressions / risks.
- The user asks to verify whether the implementation satisfies the Jira requirement referenced in the commits.
- A tech lead needs a quality gate before approve or merge.

## Goal

Produce an actionable review conclusion for a PR. The required parts — CI/checks status, bugs/regressions/defects, and Jira requirement satisfaction — plus the output structure are specified in Step 5 and the output JSON below.

## Default Principles

1. Prefer using the GitHub Enterprise API and the local codebase to perform the review; do not rely on the web page.
2. Findings come first: report bugs / risks / unmet requirements before giving the summary.
3. If no issue is found, explicitly write `No findings` and also state the remaining risks or testing gaps.
4. All external calls must have a timeout.
5. Do not leak tokens, passwords, or sensitive request headers.
6. Use `repo_graph` (query/neighbors/impact/path) on local checkouts to trace call chains and change impact before reading whole files.
7. **PR review is context-only.** Base every conclusion on the context already available: the remote PR diff, commits, reviews, comments, check status, the Jira card, and local code reading (`git_repo` read-only ops or `repo_graph`). Never attempt to execute the PR's own test suite, build, or lint (pytest, vitest/jest, `npm test`, etc.) to validate the PR — runnable proof of behavior comes from the remote checks (`github(operation="get_commit_checks", ...)`); the local repo's unit-test results are never review evidence, and running them wastes time and may alter the working tree.

## Prerequisite Reading

Before starting the review, read the following capabilities and context first:

1. `agents/skills/integration/git-pr-manager.md`
2. `agents/skills/integration/jira-analyzer.md` — requirements, acceptance criteria, implementation satisfaction from the Jira key found in the PR
3. `agents/skills/integration/git-repo-reader.md`, only when historical Jira-linked change scope is needed beyond the live PR diff
4. If the task belongs to a specific project, then read the relevant knowledge under `knowledge/`

## Input Parsing

PR review can accept any of the following anchors:

- PR URL
- owner/repo + PR number
- PR number in the current repository
- branch name
- commit SHA

If the user does not provide a specific PR anchor and instead says `review 仓库里的 PR` or `看哪些 PR 需要我 review`, enter `review-queue` mode:

1. If the user asks for all open PRs in the current repository, or says `review 仓库里的 PR`, resolve the current repository via `github(operation="get_repo_info")`/`list_prs` first, then fetch all open PRs from the current remote repository. Use the local workspace (`git_repo(operation="list_repos")` + `status`) only to learn the repository anchor when the remote cannot resolve it, or for implementation tasks.
2. If the user asks for PRs that need their review, or for the current user's open / opening PRs, first resolve the current login with `github(operation="get_user")`, then prefer `github(operation="list_prs", reviewer="<login>", state="open", all_repos=true)` using the resolved login.
3. Only use the author queue when the user explicitly says PRs created/authored by the current user; then prefer `github(operation="list_prs", author="<login>", state="open", all_repos=true)`.
4. If `list_prs` with the reviewer filter errors or is unsupported by the server, fall back to GitHub search with `is:open is:pr review-requested:<login> archived:false` (substituting the resolved login). If `list_prs` with the author filter errors, fall back to GitHub search with `is:open is:pr author:<login> archived:false`.
5. Run this skill on each PR in the queue one by one rather than only returning the PR list.
6. If no PR matches, explicitly return `No findings; no open PRs in the current remote repository.`, `No findings; no pending review PRs for the current reviewer.`, or `No findings; no open PRs authored by the current user.` according to the requested queue.

If the user does not explicitly provide a Jira key, it must be reverse-located from the following sources:

1. PR title
2. branch name
3. PR body
4. commit messages

Recommended Jira key regex:

```text
[A-Z][A-Z0-9]+-\d+
```

If multiple Jira keys are extracted, determine the primary key using the main title, branch name, and commit frequency. If it still cannot be determined, you must tell the user that the reference is ambiguous.

## Execution Steps

### Step 0: Discover the Review Queue (repo-level review only)

When the input is not a single PR but a repo-level review, follow the queue-discovery rules in Input Parsing above: resolve the current login via `github(operation="get_user")`, then use `github(operation="list_prs", reviewer="<login>", state="open", all_repos=true)`; filter by author only when the user explicitly asks for PRs authored/created by the current user. Run the subsequent review steps only on the matched PRs.

### Step 1: Inventory the PR Basics

Fetch at least: repo, PR number, title, author, base / head, state / draft, head SHA, changed files, commits.

Use the `github` tool:

```json
github(operation="get_pr_detail", repository="<owner/repo>", number=<N>)
```
This returns PR metadata, changed files, commits, and head SHA in a single call.

### Step 2: Check Whether `all checks have passed`

For the PR head SHA, inspect both categories of status:

1. combined status
2. check runs

Use the `github` tool:

```json
github(operation="get_commit_checks", repository="<owner/repo>", sha="<HEAD-SHA>")
```
This returns both the combined status and check runs in a single call.

Decision rules:

- If the combined status is not `success`, treat that as not fully passed by default.
- If any check run has `status != completed`, treat it as still running or pending; do not conclude that everything passed.
- Any check run with conclusion `failure`, `timed_out`, `cancelled`, `action_required`, or `startup_failure` means not fully passed.
- `success`, `neutral`, and `skipped` are non-blocking.
- If the repository's required checks cannot be read directly, do not fabricate a `required passed` conclusion; report only whether the observed checks all passed or whether failures / pending checks remain.

The output must list at least:

- overall conclusion: passed / not passed / inconclusive
- failing or pending check names
- if all checks are green, also state which head SHA was checked

### Step 3: Perform the Code Implementation Review

Static review only — base the review on the PR diff, the local codebase, and the remote checks. **Do not execute the repository's test suite, build, or lint to validate the PR.** Focus on:

1. Control flow truly covers the main requirement path.
2. Missing exceptions, null handling, boundary conditions, or permission checks.
3. Inconsistent fields across the changed API / DTO / UI.
4. Old callers, tests, configuration, exports, and scheduled jobs on the call chain updated together.
5. Implementation changes only the happy path, leaving negative paths uncovered.
6. Timeout, retry, polling, and asynchronous waiting behavior follows repository rules.
7. Obvious errors in logging, error handling, return values, or branch conditions.
8. Necessary tests missing, or existing tests unable to cover the new behavior.

If a problem cannot be determined from the diff alone, prefer one hop to the code that directly controls that behavior rather than doing a broad search or running the code.

### Step 4: Fetch the Jira Requirement and Align the Implementation

After a Jira key is identified from the PR / commits:

1. Read the Jira issue details.
2. Extract summary, description, acceptance criteria, labels, and key business rules.
3. Read Jira comments when necessary to confirm additional scope or clarification.
4. Map the Jira requirements one by one to the files, interfaces, pages, and behaviors changed in the PR.

Focus on these deviations:

- A key scenario required by Jira was not implemented.
- The implementation used the wrong status transition, field mapping, or permission model.
- Jira required UI behavior, but the PR only added API work or only changed text.
- Jira required boundary / exception handling, but the PR only handled the main path.
- The Jira key referenced in commits is obviously inconsistent with the actual module being changed.
- The PR includes extra changes that have no corresponding Jira requirement or explanation.

### Step 5: Output the Review Conclusion

Default output order:

1. Findings
2. Open questions / assumptions
3. Check summary
4. Jira alignment summary
5. Brief review report

Each finding must include at least:

- severity
- file / symbol / API / behavior anchor
- why it is a problem
- what user-visible or integration-visible impact it may cause

If the user explicitly asks for approve / merge decision support, add one clear recommendation:

- `approve-safe`
- `approve-with-risks`
- `do-not-approve`

The relationship between `recommendation` and the approval gate is:

- `approve-safe`: treat the review as passed; approval is allowed to proceed by default.
- `approve-with-risks`: treat the review as not passing the default approval gate; skip approval by default and return the risks to the user as feedback.
- `do-not-approve`: explicitly disallow default approval; approval must be skipped and blocking findings must be returned.

### Brief Review Report

At the end of every completed code review, include a concise `briefReport` — structure per the JSON example below. Keep each item short; when uncertain, list only a few high-signal uncertainty points or deviation points rather than a long inventory. The five items:

1. `Context completeness (confidence)`: whether the review context was complete enough to judge the PR (PR diff, related local code paths, checks, Jira issue, Jira comments, project knowledge available).
2. `Jira certainty`: whether the Jira card was clearly identified and its content sufficient; if not, list the few uncertain points.
3. `Final code vs card consistency`: whether the final code appears consistent with the Jira card; if not, list a few possible deviations.
4. `Project convention consistency`: whether the PR follows repository and project conventions; if not, list a few convention deviations.
5. `Other potential issues`: a few additional risks (regression, observability, migration/configuration, rollout, unverified edge cases).

## Suggested Findings Severity

- `high`: causes incorrect results, data errors, permission errors, main-flow failure, or a key unmet requirement
- `medium`: clear regression risk, missing boundary handling, incomplete exception handling, or a significant testing gap
- `low`: readability issues, minor consistency issues, low-probability risk, or a non-blocking defect

## Recommended Output Structure

```json
{
  "checks": {
    "headSha": "abc123",
    "overall": "not passed",
    "failing": ["build", "unit-tests"],
    "pending": []
  },
  "jira": {
    "key": "PROJ-1234",
    "summary": "...",
    "alignment": "partial"
  },
  "briefReport": {
    "contextCompleteness": { "confidence": "medium", "note": "PR diff and Jira were available; related downstream service was not inspected." },
    "jiraCertainty": { "status": "clear", "uncertainPoints": [] },
    "codeVsCardConsistency": { "status": "partial", "possibleDeviations": ["Missing negative permission case"] },
    "projectConventionConsistency": { "status": "mostly consistent", "possibleDeviations": ["No focused regression test added"] },
    "otherPotentialIssues": ["Manual rollout behavior not verified"]
  },
  "findings": [
    {
      "severity": "high",
      "title": "Missing permission guard on reviewer action",
      "anchor": "src/pages/detail/index.vue",
      "impact": "non-authorized users can see the action"
    }
  ],
  "recommendation": "do-not-approve"
}
```

If this is a review queue, it is recommended to wrap the results as:

```json
{
  "repo": "your-org/your-repo",
  "openPrs": 2,
  "results": [
    {
      "pr": 123,
      "checks": { "overall": "passed" },
      "findings": [],
      "recommendation": "approve-safe"
    }
  ]
}
```

## Verified Experience

- Checks all green only means automated validation passed; it does not mean the implementation satisfies Jira — report check status and requirement satisfaction separately.
- After extracting the Jira key from the commit / branch / PR title and then aligning the requirement, it is easier to discover that the implementation is off-target or only half-finished than by looking only at the PR diff.
- PR review never needs to execute the repository's tests. The review judgment comes from the context: diff + code reading + remote checks + Jira requirements. If the checks are green, CI already validated behavior; if they are not, report the failing checks instead of trying to reproduce them locally.
