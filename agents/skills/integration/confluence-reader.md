---
slug: "integration/confluence-reader"
description: "Pull page content from Confluence and convert it into knowledge entries; suitable for single-page learning, knowledge-base page tree traversal, extracting flow / permission / architecture documents, and incrementally merging Confluence content back into the project knowledge directory"
---

# Skill: Confluence Reader

## Trigger Conditions

- The user provided the `--confluence-page` parameter.
- The user provided a Confluence knowledge-base root page rather than a single detail page.
- The current project knowledge is missing necessary background.
- The knowledge contains entries marked as needing updates.
- The user explicitly asks to continue learning Confluence content, for example `继续学 Confluence 里的流程 / 权限 / 架构 / 数据结构`.

## Execution Steps

Use the `confluence` tool for all Confluence API calls. Authentication is handled automatically from user tokens.

**Tool calls:**
```json
confluence(operation="get_pages", page_ids=["<PAGE-ID>"])
confluence(operation="get_pages", page_ids=["<ID-1>", "<ID-2>", "<ID-3>"])
confluence(operation="get_page_tree", root_page_id="<ROOT-PAGE-ID>")
confluence(operation="get_page_tree", root_page_id="<ROOT-PAGE-ID>", include_body=true, max_pages=50)
confluence(operation="search", cql="ancestor=<ROOT-PAGE-ID> and type=page")
```

1. Confirm the learning scope:
   - Single-page ID → fetch the body directly with `confluence(operation="get_pages", page_ids=["<PAGE-ID>"])`.
   - Knowledge-base root → enumerate the index with `confluence(operation="get_page_tree", root_page_id="<ROOT-PAGE-ID>")` first, then fetch key child pages by topic; do not pull the whole tree's bodies at the start.
2. Tree enumeration: prefer the stable approach — `confluence(operation="get_page_tree", ...)` internally uses `content/search?cql=ancestor=<rootPageId>`. Do not rely on `/rest/api/content/{id}/descendant/page` by default (returns 500 in some environments).
3. Page-body fetching: batch IDs in one call — `confluence(operation="get_pages", page_ids=["id1", "id2", "id3"])`. All pages under a root → `confluence(operation="get_page_tree", root_page_id="...", include_body=true, max_pages=50)`, then extract and merge durable facts. Oversized page → index titles/IDs first, then read key pages in smaller batches.
4. The tool strips HTML tags (headings and lists preserved) and repairs mojibake (latin1→utf8); if content still looks garbled, stop writing that page and mark it for manual confirmation.
5. Extract these dimensions:
   - **Project context**: business goals, user roles, system boundaries
   - **Glossary**: domain terms, abbreviations
   - **UI page map**: page paths → feature-module mappings
   - **Business rules**: validation logic, flow constraints, permission requirements
   - **Permission / role model**: role family, org scope, action/group/system relationships, verified account anchors
   - **Architecture / data anchors**: core services, key tables, config root causes affecting flow branches
   - **Test-related constraints**: environment-specific behaviors, data dependencies, time windows
6. Write only verifiable, automation-valuable content: stable rules, state machines, key fields, accounts, permission boundaries. Without exact enum names or runtime evidence, mark as `inferred` / `partial`, never verified fact.
7. Write results into the corresponding knowledge files (`context.md`, `glossary.md`, `page-map.md`, `test-patterns.md`, `permission-matrix.md`, `role-matrix.md`).

## Output Format

Write to the corresponding `.md` files under `knowledge/`. On each update, append one line to `history.md` in this format:

```
- {date} | {page-title} | updated {knowledge files}
```

## Deduplication Strategy

- Same term/rule present → merge, don't append duplicates.
- Conflicting content → keep the latest, annotate the older source.
- Never copy full Confluence paragraphs; compress into rules, matrices, anchors, or test-guidance statements first.
- Template-state files → fill the minimum usable baseline before details.

## Recommended Batch Learning Order

1. `context.md` first: main business flow, key roles, system boundaries.
2. Then `glossary.md` / `page-map.md`: unify terminology and page entry points.
3. Then `test-patterns.md`: convert flow rules into testable design constraints.
4. Permission/role docs → `permission-matrix.md` / `role-matrix.md`.
5. Architecture/ER/data-dictionary pages → merge stable service and table anchors into `context.md`.

## Error Handling

- Confluence unreachable → tell the user to check credentials; do not write incorrect data.
- Empty page body → skip it and record that in history.
- Root body empty but child pages have content → do not stop; continue the ancestor index plus child-page fetch flow.
- Descendant API returns 500 → switch to `content/search?cql=ancestor=<rootPageId>`; do not repeatedly retry the failing path.
- Page-body fetch too large (buffering/output issues) → two-stage flow: index first, then fetch targeted page bodies in batches.
- Encoding corruption → try decoding repair first; if unfixable, stop writing that page and mark it for manual confirmation.
