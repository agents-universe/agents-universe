---
slug: "knowledge/knowledge-manager"
description: "Manage the acquisition, accumulation, update, and retirement of project knowledge"
tools:
  - user_confirm
  - knowledge_rw
---

# Skill: Knowledge Manager

## Responsibilities

Maintain the knowledge files under `knowledge/` so the agent keeps getting smarter across repeated runs.

Use `knowledge/_template/` as the baseline file set. Projects are initialized with a per-category subset of the templates (see `knowledge/categories.yaml` — every category lists its own subset, e.g. `software` lists all software-relevant files, `customer-service` adds the 客服 files); a project may add or remove files freely afterwards, but removals must go through `knowledge_rw(operation="delete", slug="...")` so the knowledge index stays in sync. If a project knowledge directory contains additional established files, keep this reference table in sync rather than treating them as ad hoc notes.

To initialize a project knowledge directory, use `filesystem(operation="create_dir", path="knowledge")` then create the template files via `knowledge_rw(operation="write", ...)`. For routine updates, read/write Markdown files directly with `knowledge_rw` and append to `history.md`.

## 知识写入优先级（第一原则）

每次「学习/沉淀」知识时，按以下顺序决定写入位置：

1. **优先更新已有文件**：内容能归入项目 `knowledge/` 中任一已有文件时（对照下方参考表；用 `knowledge_rw(operation="list")` 查看项目实际文件清单），直接更新该文件 —— 包括内容仍是占位符（如 `(to be filled …)`）的模板实例文件。占位符文件的存在本身就表示该项目预期维护这份知识；把它填上，不要新建平行文件。
2. **仅当无文件可归入时才创建新文件**：通过下方「Knowledge Write Eligibility」严格判断；新文件必须属于一个明确类别，且预期会在未来不同需求中被引用。既定合法例外：`technical/api/{service-slug}.md` 明细文件（每个服务一个，见「API Documentation: Mandatory Two-Level Structure」）。
3. 不得仅为「主题更具体」而绕过已有文件新建平行文件 —— 先扩展现有文件的 section；只有当主文件超过 500 词或触发层级拆分条件时，才拆出 detail 文件。

## Knowledge File Reference

| File | Contents | Update Frequency |
|------|----------|------------------|
| `context.md` | Overall background, business goals, architecture summary | Establish early, update infrequently |
| `glossary.md` | Domain terms and abbreviations | Append when new terms appear |
| `login-and-user-switch.md` | Login entry points, user/company/tenant switching modes, verified accounts | After login-flow or permission corrections |
| `page-map.md` | Page routes -> functional module mapping | When requirements change |
| `ui-patterns.md` | Verified UI selectors and interaction patterns | After each testing correction |
| `api-map.md` | Product-owned APIs, entry structure (index only — detail schemas in `technical/api/*.md`) | After OpenAPI/service discovery; **MUST also create/update detail files** |
| `kong-map.md` | Kong/gateway relative paths, accessName fallback (index only — detail in `technical/kong/*.md`) | When new routes are added; **MUST also create/update detail files** |
| `integrations/custom-api.md` | Customer-owned and third-party API integration catalog: base URLs, allowed hosts, auth secret refs, endpoint catalog, usage rules | During onboarding; when API systems/endpoints change |
| `permission-matrix.md` | Entitlement -> menu, page action, API, data-scope, or masking mappings | After permission docs, UI observation, API/code discovery, or role-difference checks |
| `role-matrix.md` | Executable role archetypes, observed accounts, capability bundles, minimum entitlements | After account discovery, role exploration, or permission-baseline corrections |
| `test-patterns.md` | Reusable testing strategies and patterns | Extract after design is completed |
| `data-source-map.md` | Data source inventory: type, environment, access via `secret_ref`, owner, refresh frequency (data-analysis projects) | During onboarding; when sources or access change |
| `data-model.md` | Core tables with layer/granularity, fact-dimension relationships, data quality rules | When table model or quality rules change |
| `data-pipelines.md` | ETL/ELT job inventory, schedules, dependencies, backfill and failure handling | When jobs or schedules change |
| `metric-catalog.md` | Authoritative metric definitions (formula, dimensions, source tables) and conflict rulings | When metric definitions are added/changed or conflicts ruled |
| `analysis-scenarios.md` | Recurring reports/dashboards inventory and ad-hoc analysis log | When reports change; append after each thematic analysis |
| `sql-patterns.md` | SQL dialect/engine, conventions, reusable snippets (retention/funnel/sessionization), performance notes | Extract after recurring query patterns are verified |
| `analysis-patterns.md` | Analysis method selection, chart selection rules, report structure | When methodology conventions are established or corrected |
| `faq.md` | Customer-service FAQ: one Q&A per topic, question-formatted headings, negative examples ("we do not provide X"); answers must trace to service policies | After each new/corrected Q&A or answer correction |
| `service-policies.md` | Service policies and the "not provided" list — the authoritative anti-hallucination fact source | When policies, scope, or rules change |
| `escalation-rules.md` | Escalation triggers, human channels/contacts, handoff requirements, service metric targets | When channels, SLA, or targets change |
| `support-scripts.md` | Customer-service reply templates: openers, answer structure, closers, sensitive scenarios | When scripts are optimized |
| `history.md` | Knowledge update log | Append on every change |

## Knowledge Sources

1. **Pulled from Confluence**: transformed from documents via the `confluence-reader` skill
2. **Execution feedback**: extracted from diffs after the user corrects generated code
3. **Design insights**: from `designInsights` output by `test-designer`
4. **Manual additions**: the user edits knowledge files directly
5. **Guided acquisition**: structured interviews by the `project-customization-expert` agent, who asks the user questions and writes structured knowledge files from the answers

## Knowledge Write Eligibility

Before writing to any knowledge file, answer one question:

**"Would an agent working on a completely different requirement for this project need this content?"**

If yes — write it. If no — keep it in the current task context only; do not create or update a knowledge file.

### Content that qualifies

| Category | Examples |
|----------|---------|
| Business rules | Validation logic, state machine transitions, workflow constraints |
| Architecture & system structure | Service boundaries, module relationships, deployment topology |
| API contracts | Endpoints, request/response schemas, error codes, auth patterns |
| Page & route map | URL routes, page entry points, feature-module mappings |
| Permission & role model | Entitlement IDs, role capabilities, org-scope rules |
| UI interaction patterns | Verified selectors, navigation patterns, form interactions |
| Test patterns & strategies | Reusable test dimensions, data setup rules, regression risk areas |
| Domain glossary | Terms, abbreviations, domain-specific meanings |
| Data sources, models & pipelines | Table structures, metric definitions, pipeline schedules |
| Integration catalog | Third-party API base URLs, endpoint catalog, auth types |

### Content that does NOT qualify

- A finding specific to one Jira card or requirement (e.g. "the button on QA-123's screen is broken")
- A one-off debugging result that applies only to the current defect
- Intermediate task state or progress notes
- Execution evidence (test results, screenshots, log output)
- Information that is likely to change before it would be reused

### Updating existing files vs. creating new files

**默认更新。** 先 `knowledge_rw(operation="list")` 确认项目已有文件；有可归入的文件就直接更新（含占位符空模板），确认无文件可归入后才进入下面的新建判断。

- **Updating an established file** (adding a selector to `ui-patterns.md`, correcting a route in `page-map.md`, appending a new API endpoint to `api-map.md`): lightweight check — if the update fits the file's declared category above, proceed.
- **Creating a new knowledge file**: apply the eligibility question strictly. A new file must clearly serve a named category above and be expected to be referenced in future sessions for different requirements.

## Update Rules

Scoping reminder:

- When adding a new durable knowledge file to `knowledge/_template/` or a project's `knowledge/` directory, also update this reference table (ownership + update trigger).
- Do not expand knowledge into a full schema catalog by default.
- For self-adapt DB or other Kong-backed DB fallback routes, record only the tables automation actually touches, and only when their purpose matters for test design, traceability, or Jira explanation.
- If table meaning is still unknown, keep a short placeholder note instead of inventing a purpose.

### Additions
- Append new entries at the end of the file; use `##` headings for categorization; add a source marker to each entry: `<!-- source: {confluence-page-id | issue-key | manual} -->`

### Merges
- Keep only one entry per term or concept, retaining the latest explanation; when the new knowledge conflicts with the old one, **delete the old entry entirely** -- do NOT preserve it as a `<!-- deprecated: ... -->` comment, because a stale conflicting entry can still mislead later runs.

### Retirement
- Mark entries not referenced for more than 90 days as `[stale]`; delete only after user confirmation.
- Remove a knowledge file with `knowledge_rw(operation="delete", slug="...")` — it deletes the file and its database index row together (do NOT use `filesystem(delete_file)` for knowledge files).
- If the knowledge list shows entries whose files no longer exist (residue left by earlier file deletions), clean them up with `knowledge_rw(operation="purge")` for the whole project, or `knowledge_rw(operation="purge", slug="...")` for a single stale row.

### Template Pruning
When a project is not a software/testing project, some template files may be irrelevant.
- Do NOT delete them silently. Add `status: not_applicable` to frontmatter, note the reason in the body, and use `user_confirm` first.
- If a template can be repurposed (e.g., `test-patterns` -> `quality-checklist`), rewrite its content while keeping the slug.
- Always record pruning decisions in `system/history`.

## Hierarchy Management

### Concepts

Knowledge files can be organized into a parent/child tree with three levels:

- **Index files** (`knowledge_level: index`): Always loaded at project start. Navigational maps pointing to detail files. Body stays under the 500-word limit (Maintenance Rules).
- **Detail files** (`knowledge_level: detail`): Deferred — loaded on-demand via `knowledge_rw(operation="load", slug="...")`. In-depth content.
- **Auto files** (`knowledge_level: auto`): Default. Loaded at depth 0 (no parent); deferred with a parent at depth > 0.

Maximum hierarchy depth: 5 levels.

### Index File Frontmatter

```yaml
---
knowledge_level: index
slug: "technical/api-map"
title: "API Map"
category: technical
summary: "Service inventory and endpoint catalog for the product APIs"
children:
  - "technical/api/users-service"
  - "technical/api/orders-service"
  - "technical/api/auth-service"
tags: [api, endpoints, index]
---
```

### Detail File Frontmatter

```yaml
---
knowledge_level: detail
slug: "technical/api/users-service"
title: "Users Service API"
category: technical
parent: "technical/api-map"
summary: "CRUD operations for user management (/api/v1/users)"
tags: [api, users]
---
```

### When to Create an Index (Trigger Conditions)

1. **Primary file grows large**: A main (non-detail) file is loaded into context every turn — keep it under the 500-word limit (Maintenance Rules below); split into an index summary + detail files when it grows beyond that.
2. **4+ distinct sub-topics in one file**: Sections that could be independently referenced warrant their own detail files.
3. **5+ related files in a category**: A directory accumulates many related flat files — introduce an index for navigation.
4. **Selective loading needed**: Agents frequently load a file but only use a subset of its content.
5. **Total primary files exceed 12**: Consider merging related files or demoting low-frequency files to `knowledge_level: detail` with a parent index.

Do NOT create hierarchy preemptively. Prefer flat files until a trigger condition is met.

### How to Split a Flat File into Hierarchy

1. **Assess**: Identify logical sub-sections that form independent knowledge units.
2. **Create the index file**: `knowledge_level: index` at the original slug. Body = summary table/list with one-line descriptions of each child.
3. **Create detail files**: Per sub-section, `knowledge_level: detail` with `parent` pointing to the index slug.
4. **Update the index's `children` list** to list all child slugs.
5. **Verify symmetry**: Every child's `parent` must match the index; the index's `children` must list every child.
6. **Append to `history.md`**: Record the hierarchy creation.

Use the index file body format below as the reference for new index files.

### Index File Body Format

```markdown
# Topic Area

One-paragraph overview of the topic area.

## Contents

| Entry | Slug | Summary |
|-------|------|---------|
| Users Service | [[technical/api/users-service]] | User CRUD, profile, preferences |
| Orders Service | [[technical/api/orders-service]] | Order lifecycle, fulfillment |

## Related

- [[domain/context]] — business context
- [[technical/kong-map]] — gateway routing
```

### API Documentation: Mandatory Two-Level Structure

**THIS IS A HARD REQUIREMENT, NOT A SUGGESTION.** When learning API documentation (Swagger/OpenAPI, service endpoints), you MUST create two levels. Failure to do so means the task is NOT complete.

**Level 1 — `api-map.md` (primary, auto-loaded)**
Keep as a concise catalog/index ONLY. Contains per-service:
- Service name, base path, OpenAPI URL
- Endpoint list: `METHOD /path` — one-line purpose description
- Operation counts, schema counts
- `[[technical/api/{service-slug}]]` cross-reference link to the detail file

**FORBIDDEN in api-map.md:** request bodies, response bodies, field schemas, error code tables, parameter details. These MUST go in level-2 files.

**Level 2 — `technical/api/{service-slug}.md` (detail, on-demand) — MANDATORY**
You MUST create one detail file per service. This is not optional. Each file contains:
- Full request body schemas (field-by-field with types, constraints, examples)
- Full response body schemas
- Error codes and meanings
- Query parameter details
- Authentication/authorization requirements
- Example request/response pairs

Frontmatter for each level-2 file:
```yaml
---
knowledge_level: detail
slug: "technical/api/{service-slug}"
title: "{ServiceName} API Detail"
category: technical
parent: "technical/api-map"
summary: "{one-line: what this service does and key endpoints}"
tags: [api, {service-tag}]
---
```

**Completion checklist (ALL required before reporting success):**
1. [ ] Each service has a `technical/api/{service-slug}.md` file with `knowledge_level: detail`
2. [ ] `api-map.md` frontmatter `children` list includes all child slugs
3. [ ] `api-map.md` body has `[[technical/api/{service-slug}]]` links for every service
4. [ ] Each detail file has `parent: "technical/api-map"` in frontmatter
5. [ ] `history.md` records the hierarchy creation

If you write API content to `api-map.md` without creating the corresponding detail files, the system will warn you and the task is considered INCOMPLETE.

The same pattern applies to `kong-map.md` — if Kong route details (full request/response mappings) grow large, split into `technical/kong/{route-group}.md` detail files with `parent: "technical/kong-map"`.

### Refreshing / Relearning API Documentation

When the user asks to refresh, relearn, or re-fetch Swagger/API docs, you MUST:

1. **Update `api-map.md`** — refresh the service catalog, endpoint list, counts
2. **Update each existing detail file** — re-fetch that service's OpenAPI spec and update the request/response schemas in `technical/api/{service-slug}.md`
3. **Create NEW detail files** — for new services without one yet
4. **Remove stale entries** — mark defunct services' detail files `[stale]` and drop them from `api-map.md` children

A refresh that only updates `api-map.md` without touching detail files is INCOMPLETE — the detail files hold the actual schemas used for test generation and API interaction; they must stay in sync.

**Quick check after refresh:**
- `ls knowledge/technical/api/` should have one `.md` file per service listed in `api-map.md`
- Each detail file reflects the latest OpenAPI spec
- `api-map.md` `children` list matches the actual files

### Navigating Hierarchy at Runtime

- `knowledge_rw(operation="list", root_only=true)` — see top-level files only
- `knowledge_rw(operation="children", slug="parent/slug")` — explore children of an index
- `knowledge_rw(operation="status")` — see which detail files are available as deferred
- `knowledge_rw(operation="load", slug="detail/slug")` — bring a detail file into context
- `knowledge_rw(operation="unload", slug="detail/slug")` — release when done

### Maintenance Rules

- An index file body MUST NOT exceed 500 words. If it grows larger, it is trying to be a detail file.
- A detail file's `parent` MUST reference an existing index file (symmetry rules in the split steps above).
- If a child file is retired, remove it from the parent's `children` list, then delete the child file via `knowledge_rw(operation="delete", slug="...")`.
- If an index file is retired, promote or flatten its children first.
- When a detail file is loaded, prefer also reading the parent index for context orientation.

## `history.md` Format

```markdown
## Update Log

- 2026-05-15 | confluence:12345 | Updated context.md, glossary.md
- 2026-05-15 | issue:QA-123 | Added ui-patterns.md#order-form
- 2026-05-16 | feedback | Corrected the /orders path in page-map.md
```

## Third-Party Integration Setup

When a project needs third-party system access (issue tracker, source control, API gateway, documentation, IAM, etc.):

### Non-secret config (base URLs, project keys, system names)

1. Ask the user with `user_confirm(kind="text", ...)`.
2. Save to project-scoped personal memory: `memory_rw(operation="save", memory_type="project_setting", key="...", value="...", domain="...")`.
3. Do NOT write customer-specific config into `knowledge/environment/environment.md` or any framework template.

**Exception — Kong base URL:** the `kong` tool parses its base URL from the env block of `knowledge/environment/environment.md` (keys like `KONG_BASE_URL_DEV`, `KONG_BASE_URL_UAT`), not from personal memory or env vars. For Kong only, write those keys into that file with `knowledge_rw`.

### Third-party API endpoint catalogs

For stable integration definitions (base URLs, allowed hosts, auth types, endpoint catalogs), update `integrations/custom-api.md` via `knowledge_rw` — shared project knowledge all agents use to call the system.

### Secrets (tokens, passwords, API keys)

Never collect or store secrets in knowledge files or personal memory. Use:
- `user_confirm(secret=true, service_key="...", save_to_project_secrets=true)` for interactive collection
- Or let tools like `api_request` / `kong` handle secure prompts automatically

### Legacy environment.md

If `knowledge/environment/environment.md` already holds non-secret config from a previous setup, you may read it for backward compatibility; all new config goes to project-scoped personal memory. If you find secrets in environment.md, warn the user and suggest migrating to project secrets.

## Triggers

- 用户明确要求学习/沉淀/回写知识（如「知识沉淀」「写回知识」「更新知识库」）
- 各 agent 工作流中的知识回写步骤（如 QA 流程 Step 9、pentest Phase 6）
- `knowledge-ingestion` workflow（URL / Confluence 内容入库）
- 执行反馈提炼：用户纠正生成代码后，从 diff 提取可复用知识（见 Knowledge Sources 第 2 条）
