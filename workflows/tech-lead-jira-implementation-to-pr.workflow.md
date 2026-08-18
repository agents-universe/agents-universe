---
slug: "tech-lead-jira-implementation-to-pr"
description: "Tech Lead workflow for implementing a Jira issue through a clean Git baseline, tested feature branch, and pull request"
agent: "tech-lead"
triggers:
  - "implement Jira issue"
  - "develop Jira card"
  - "Jira implementation to PR"
tools:
  - git_repo
  - filesystem
  - user_confirm
  - github
---

# Tech Lead Jira Implementation to PR

Use this workflow when the Tech Lead implements a Jira issue and delivers a pull request. Follow the sequence exactly for every target repository.

## 1. Requirements and repository scope

1. Load `agents/skills/integration/jira-analyzer.md` and identify the Jira summary, acceptance criteria, affected modules, and repositories.
2. If a repository cannot be determined, ask the user; do not guess.
3. For an existing checkout, run the clean gate before any checkout, pull, or file operation. Stop on staged, unstaged, untracked, or unexpected changes; never overwrite user work.

## 2. Clean baseline and feature branch

For each repository, in this order:

1. Run `git_repo(operation="status", repository="<owner/repo>")` as the clean gate.
2. Prepare the feature branch with `git_repo(operation="branch_prepare", repository="<owner/repo>", branch="feature/{JIRA}")`. This tool fetches latest `main`, creates or reuses the feature branch, and fast-forwards automatically. A non-fast-forward result blocks the repository.

Never use a user-name branch. Never force-update `main`.

## 3. Implementation

1. Read the direct control path and existing test patterns.
2. Implement the intended scope directly on `feature/{JIRA}` — no confirmation before writing code; the plan (repositories, exact paths, approach, acceptance-criteria coverage) goes into the final report.
3. Do not modify generated files, secrets, lockfiles, or unrelated user changes.

## 4. First test and exact-path commit

1. Run the first applicable test immediately after implementation and record the command and result.
2. A database or Redis setup/dependency failure may be classified as `environment-blocked`; record the affected test, skipped coverage, and dependency limitation, then continue only if no blocking failure remains.
3. Compilation failures, assertion failures, and unknown failures block delivery by default. Report them to the user and ask for confirmation; with explicit user consent, delivery may continue with mandatory disclosure in the report and PR body.
4. Stage only the exact paths being committed and commit; never use a blanket add. No confirmation is required before the commit.

## 5. Final synchronization and PR

Before opening a PR:

1. Synchronize the feature branch with `git_repo(operation="sync_branch", repository="<owner/repo>", branch="feature/{JIRA}")`. This tool fetches `origin/main` and `origin/feature/{JIRA}`, merges them into the local branch, and reports conflicts explicitly. Resolve conflicts manually if reported, then re-run sync. Never rebase.
2. Run the final applicable tests with the same failure classification. `environment-blocked` must be disclosed; compilation, assertion, and unknown failures block the PR by default — report them to the user and ask for confirmation, and proceed only with explicit user consent plus mandatory disclosure.
3. Push with `git_repo(operation="push", repository="<owner/repo>", branch="feature/{JIRA}")`. Never use `--force`, `--force-with-lease`, or rebase on `main` or a shared branch. If push is rejected, run sync_branch again and retry. If push fails with a permission-denied/403 error, use the fork fallback in section 5b instead of giving up.
4. Call `github(operation="create_pr", head_branch="feature/{JIRA}", base_branch="main")` only after a successful push. If `create_pr` itself returns 403 (the account can view but cannot push to the target repository), fall back to section 5b.

If create_pr returns HTTP 422, the GitHub tool must query open PRs by exact `owner:head` and `base`. Return `already_exists` only for one match; return an explicit error for zero or multiple matches.

## 5b. Fork fallback (no push permission on the target repository)

Use this section only when the configured Git account has no write access to the target repository and the direct push / create_pr was rejected (403).

1. `github(operation="fork", repository="<owner/repo>")` — creates a fork under the configured Git account, or returns the existing fork (GitHub 202). Record the fork `full_name` (e.g. `<account>/<repo>`); the same-name fork is reused, never duplicated. GitHub creates forks asynchronously (202); if the push in step 2 fails with a not-found / repository-doesn't-exist error, wait a few seconds and retry the push once before reporting a blocker.
2. Push the feature branch to the fork: `git_repo(operation="push", repository="<owner/repo>", branch="feature/{JIRA}", target_repository="<account>/<repo>")` (fork full_name from step 1). Never `--force`.
3. Open the PR from the fork: `github(operation="create_pr", repository="<owner/repo>", head_branch="<account>:feature/{JIRA}", base_branch="main", title=..., body=...)` — the head must be `<fork-owner>:<branch>` for a cross-repo PR.
4. Star courtesy: call `github(operation="is_starred", repository="<owner/repo>")`. If `starred == false`, ask the user with `user_confirm(kind="selection", question="... 是否给 <owner/repo> 点星？", options=["是", "否"])`; only if the user agrees, call `github(operation="star", repository="<owner/repo>")`. Never star without user consent.
5. Report: fork full_name, PR URL/number, and the star decision (starred / declined / already starred).

Do not approve or merge the PR. Report PR URL/number, exact changed paths, test results, any `environment-blocked` limitation, and blockers.
