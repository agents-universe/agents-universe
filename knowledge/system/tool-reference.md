---
slug: "system/tool-reference"
title: "Built-in Tool Reference"
category: "system"
tags: ["tools", "reference"]
---

# Built-in Tool Reference

All tools are registered in `packages/agent-core/src/agent_core/tools/registry.py`.

## filesystem

Read/write/list/delete files on the server filesystem within allowed paths.

```
Operations: read_file, write_file, list_dir, delete_file, create_dir
Constraint: paths must be within project's fs_path or knowledge root
```

## web_fetch

Fetch content from a URL via HTTP GET.

```
Inputs: url (str), timeout_seconds (int, default 30)
Output: {content: str, status_code: int, content_type: str}
```

## browser_playwright

Controls a headless Chromium browser; returns screenshots and page state.

```
Operations: goto, click, fill, screenshot, wait_for_selector, evaluate
Output includes: {screenshot_path: str, page_title: str, page_url: str}
```

## code_executor

Execute Python code in a sandboxed subprocess. 30s timeout. No network access.

```
Inputs: code (str), language (python|bash)
Output: {stdout: str, stderr: str, exit_code: int, images: [...]}
```

For image output, code must write PNG files to `/tmp/output_{n}.png`.

## knowledge_rw

Read/write/load/unload/refresh/navigate Markdown knowledge files; hierarchical organization.

```
Operations: read, write, load, unload, refresh, status, list, search_by_slug, children
Write creates a version entry in knowledge_versions table and triggers reindex.
Slug format: "{category}/{filename-without-extension}"
```

### Hierarchy operations

- `list(root_only=true)` — show only top-level files (no parent)
- `children(slug="parent/slug")` — list immediate children of an index file
- `status` — shows static-loaded, dynamically loaded, and deferred (detail files available for loading)
- `load(slug="...")` — bring a deferred detail file into persistent context
- `unload(slug="...")` — release a dynamically loaded file from context
- `refresh(slug="...")` — re-read an in-context file after external update

### Hierarchy frontmatter fields

- `knowledge_level`: `index` (always loaded, navigational map), `detail` (deferred, loaded on demand), `auto` (default — loaded if depth 0, deferred if depth > 0)
- `parent`: slug of the parent index file
- `children`: list of child slugs (declared in the index file)
- `summary`: one-line description shown in deferred listings and children queries

## sql_query

Run read-only SQL queries against the app database, or trigger server operations.

```
Inputs: query (str), params (dict), operation (str, optional)
Output: {rows: list[dict], row_count: int}
Only SELECT queries allowed. Uses a read-only DB connection.
```

### Special operations

- `operation: "reindex_knowledge"` — reindex project knowledge files
  - No params → full reindex of the project knowledge directory
  - `params: {fs_path: "category/filename.md"}` → reindex a single file (relative to knowledge dir)

## shell

Run shell commands (bash) in a restricted sandbox.

```
Inputs: command (str), cwd (str, optional, relative to project root), timeout_seconds (int, default 30, max 300)
Allowed: git, ls, cat, grep, find, jq, echo, pwd, head, tail, wc, sort, uniq, diff, mkdir, cp, mv, npx, npm, node, python, java, javac, mvn, ./mvnw, ./gradlew
  Note: ./mvnw and ./gradlew only work from the project directory containing those wrapper scripts.
  java/javac/mvn are available in the default API container (JDK 21, Maven). gradle is NOT globally installed; use ./gradlew.
Blocked: rm -rf, sudo, curl, wget, pip install, apt-get, xargs
Output: {stdout: str, stderr: str, exit_code: int}
```

Java/Spring Boot tests: detect in order `./mvnw test` → `mvn test` → `./gradlew test`; use the first applicable. Raise `timeout_seconds` (up to 300) for Maven/Gradle builds. Exit codes are faithful — environment failures must be classified by the agent, not the tool.

## git_repo

Clone, checkout, pull, branch, commit, push, search, inspect local checkouts scoped to the project workspace. Authentication injected automatically; force push not supported.

```
Operations: clone, checkout, pull, status, search, log, show, blame, list_repos, unshallow,
            branch_create, branch_prepare, sync_branch, commit, push

Inputs (choose one per call):
  repository      — remote "owner/repo" used only for clone (resolves to repos/<name>/ in workspace)
  repository_path — project-relative path to an existing checkout; supports any directory inside
                    the project workspace, not just repos/<name>; rejected if it escapes the workspace

Key operations:
  status          — returns branch, head SHA, clean/dirty flag, staged/unstaged/untracked/unmerged file lists
  checkout        — switch to an existing local branch; requires a clean tree; returns branch and head SHA
  pull            — requires clean tree; uses --ff-only; returns before/after SHA
  branch_prepare  — safe branch creation for Jira delivery:
                    1. dirty-tree gate (stops if uncommitted changes exist)
                    2. fetch origin
                    3. checkout main, git pull --ff-only origin main
                    4. create feature/{JIRA} from updated main, or reuse/track existing remote branch
                    returns branch, action (created|reused_local|tracked_remote), head SHA, ahead/behind
  sync_branch     — fetch + merge origin/main and origin/feature/{JIRA} into current feature branch;
                    returns conflict files on conflict (stop and resolve); returns merged refs and ahead/behind
  commit          — requires non-empty paths list (exact repository-relative paths); stages only those
                    paths; rejects commit if other files are already staged; returns SHA and files list
  push            — ordinary push only; returns status "rejected" with sync instructions on non-fast-forward;
                    force_with_lease is not available
```

Force push permanently disabled. Non-fast-forward push → run sync_branch, resolve conflicts, retest, push normally.

## planner (plan_task)

Generate a structured task list for complex goals. Runs on the conversation's current model.

```
Inputs: goal (str), context (str, optional)
Output: {tasks: [{id, title, tools_needed, depends_on, estimated_complexity}]}
Called automatically by the agent when task mode is triggered.
```

## memory_rw

Save/recall/update/archive agent memories. Three layers: session (ephemeral), personal (persistent), episodic (past conversation summaries).

```
Operations: save, save_session, recall, recall_episodes, update, archive
Scope: project | global (for save); session | personal | all (for recall)
Never store secrets, tokens, or passwords in memories.
```

## user_confirm

Presents an interactive selection card and waits for the user's choice.

```
Inputs: question (str), options: [{label, value, description?}], field_key (str, optional), allow_other (bool, default true)
Output: {selected_value: str, field_key: str} — the chosen option value and the field key
When allow_other is true, the user can type a custom response instead of picking an option.
```

## image_annotator

Adds annotation overlays (boxes, arrows, labels) to an existing image via Pillow.

```
Inputs: image_path (str), annotations: [{type, x, y, w, h, label, color}]
Output: {annotated_path: str}
Annotation types: box, circle, arrow, label, highlight
Coordinates: pixels (integer) or percent (0.0–1.0 float)
Path: can be absolute or relative to project root.
```

## focus_template

Generates a JSON template for screenshot annotation focus areas from image dimensions.

```
Inputs: image_path (str), count (int, 1-4), units ("pixel"|"percent"), title (str), subtitle (str)
Output: {template: {title, subtitle, focus_areas: [{x, y, w, h, label}]}, image_size: {w, h}}
Path: can be absolute or relative to project root.
```

## Related Knowledge

- [[system/framework-overview]]
- [[system/skill-authoring-guide]]

## api_request

Calls a customer-configured third-party HTTP API. Auth injected server-side from encrypted secrets — plaintext never reaches the LLM.

```
Inputs:
  integration_key (required) — integration identifier from integrations/custom-api
  endpoint_key     — named endpoint in the catalog; resolved server-side:
                     supplies method default, path, per-environment base_url,
                     allowed_hosts, response_json_path, and auth defaults.
                     Prefer over raw path.
  method (required) — GET | POST | PUT | PATCH | DELETE | HEAD
  path             — relative URL path; required unless endpoint_key is used
  environment      — dev | uat | int | prd (catalog selects per-env base_url)
  path_params / query_params / json_body
  auth_type        — bearer | api_key_header | basic | cookie | custom_header | body_field | none
  secret_ref       — single secret key (e.g. third_party:crm:uat)
  secret_refs      — multi-secret map for basic/custom_header templates
  secret_scope     — "project" (project secrets then user tokens) | "user" (user vault)
  response_mode    — json | text | status | headers_only
  response_json_path — dot notation (e.g. data.items) to extract a subset
  base_url / allowed_hosts / timeout_seconds / max_response_chars / require_confirmation
Output: {status, body|ok|headers, truncated, secret_ref, catalog?}
  catalog block present when endpoint_key was used:
  {resolved_from_catalog: true, endpoint_key, resolved_environment}
```

- **Auth**: never place auth values in `headers`, `query_params`, or `json_body` — auth-named headers are rejected.
- **Confirmation**: POST/PUT/PATCH/DELETE, `prd*` environments, `require_confirmation`, or catalog `side_effect: true` trigger a user-confirm gate before sending.
- **Missing secrets**: the tool opens a secure input prompt (project secrets or user vault) — do not ask the user for plaintext tokens.
- **Catalog**: per-project at `knowledge/integrations/custom-api.md`; entry format in the template `knowledge/_template/custom-api.md`. Related skills: `integration/custom-api-onboarding`, `integration/custom-api-consumer`.

## github

GitHub Enterprise pull-request operations through the configured Git integration.

```
Operations: get_repo_info, get_user, list_prs, get_pr_detail, approve_pr, merge_pr, create_pr, get_commit_checks, add_pr_comment
Inputs for create_pr: repository (owner/repo), title, head_branch, base_branch (default main), body, draft
Output for create_pr: {success, number, url, title, head, base} or {status: "already_exists", ...}
```

For `create_pr`, a 422 response is not automatically a duplicate: the tool queries open PRs with the exact `owner:head_branch` and `base_branch`, returning `already_exists` only when exactly one matching open PR is found; zero or multiple matches return an explicit error. Never approve or merge during implementation delivery without separate authorization.
