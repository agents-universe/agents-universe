---
slug: "knowledge-ingestion"
description: "Ingest content from a URL into the current project's knowledge base"
triggers:
  - "ingest this URL"
  - "add this page to knowledge"
  - "把这个页面加入知识条目"
tools:
  - web_fetch
  - knowledge_rw
---

# Workflow: Knowledge Ingestion

## Goal
Fetch content from a given URL, structure it as a knowledge Markdown file, and index it.

## Inputs Required
- `url` — the URL to ingest

## Steps

### Step 1 — Fetch content
Use `web_fetch` to retrieve the URL content.
If the content-type is not text/html or text/plain, inform the user and stop.

### Step 2 — Map content to an existing knowledge file (do this FIRST)
Based on the content, decide its category:
- Business/process documentation → `domain/`
- API/technical documentation → `technical/`
- How-to or procedure → `skills/`

Then check whether an EXISTING project knowledge file covers this content:
1. `knowledge_rw(operation="list", root_only=true)` to see the project's current knowledge files (also visible in your context under "## Project Knowledge").
2. Cross-check the reference table in `agents/skills/knowledge/knowledge-manager.md` ("Knowledge File Reference") for the file that owns this content type.
3. If an existing file covers the topic — including an empty template instance still holding `(to be filled …)` placeholders — UPDATE it: merge the content into its sections, keep its frontmatter, append an entry to `history.md`, then go directly to Step 5.
4. Only if NO existing file covers the content, proceed to Step 3 to create a new slug.

### Step 2.5 — Hierarchy placement (MANDATORY for API content)

**For Swagger/OpenAPI/API documentation content:**

This is NOT optional. Follow `agents/skills/knowledge/knowledge-manager.md` "API Documentation: Mandatory Two-Level Structure" (the authority): `api-map.md` holds only the service catalog (endpoint list, one-line summaries); one detail file per service at `technical/api/{service-slug}.md` with `knowledge_level: detail` and `parent: "technical/api-map"`; `api-map.md` frontmatter `children` lists all child slugs; `[[technical/api/{service-slug}]]` cross-references in its body. If the detail files already exist (relearn/refresh), UPDATE them with fresh data.

**For other content:**

1. `knowledge_rw(operation="list", category="<determined-category>", root_only=true)` to check for an existing index file for this topic area.
2. If an existing index covers this content's topic:
   - Set `knowledge_level: detail` in frontmatter
   - Set `parent` to the index file's slug
   - Choose a child slug under the index (e.g., `technical/api/new-endpoint`)
   - After writing, update the parent index's `children` list to include the new slug
3. If no index exists but the content is large (> 2000 words) or covers multiple distinct sub-topics:
   - Split into an index file + detail files following the index format in `knowledge-manager.md` "Hierarchy Management" section
4. Otherwise, create a flat file with `knowledge_level: auto` (default behavior).

### Step 3 — Create a new slug only when no existing file covers the content
Generate a descriptive kebab-case slug under the category determined in Step 2.
Use `knowledge_rw(operation="list")` / `search_by_slug` to check for a similar existing file.
If yes, ask the user: "A similar file `{slug}` already exists. Update it or create new?"
(If no user answer arrives in an autonomous flow, update the existing file.)

### Step 4 — Structure the content
Apply the `knowledge/knowledge-manager` skill conventions to format the raw content into a proper Markdown knowledge file with:
- Valid frontmatter (slug, title, category, tags)
- Hierarchy fields if applicable (knowledge_level, parent, children, summary)
- Clear section headers
- Cross-links to related knowledge (`[[slug]]`)
- Gap annotations for missing information

### Step 5 — Write the file
Use `knowledge_rw` write operation with:
- `slug`: the determined slug
- `content`: the structured Markdown
- `change_summary`: "Ingested from {url}"

### Step 6 — Verify indexing
The `knowledge_rw` write operation automatically triggers reindex — no separate call needed.
Use `knowledge_rw(operation="list", category="<category>")` to confirm the new file appears and check its completeness score.

## Success Criteria
- File written to `projects/{ws}/{proj}/knowledge/{category}/`
- Completeness score ≥ 40 (amber or above)
- At least 1 cross-link present
- No duplicate content

## Error Handling
- URL unreachable → inform user, stop
- Content too large (> 10,000 words) → chunk into multiple files named `{slug}-part-{n}`
- Duplicate slug → merge or rename (ask user)
