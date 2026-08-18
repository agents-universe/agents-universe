---
slug: "system/skill-authoring-guide"
title: "Skill and Workflow Authoring Guide"
category: "system"
tags: ["skills", "workflows", "authoring"]
---

# Skill and Workflow Authoring Guide

## File Locations

| Type | Location |
|---|---|
| Global skills | `agents/skills/{category}/{slug}.md` |
| Project-specific skills | `projects/{ws}/{proj}/knowledge/skills/{slug}.md` |
| Workflows | `workflows/{slug}.workflow.md` |
| Skill mixins | `agents/skills/_mixins/{slug}.md` |

Project skills override global skills with the same slug.

## Frontmatter Fields

```yaml
---
slug: "category/skill-name"       # required, unique identifier
type: "guidance"                   # required: guidance|template|executable|composite
description: "One-line description"  # required
triggers:                          # optional: phrases that cause auto-loading
  - "review this code"
tools:                             # tools this skill needs
  - filesystem
  - web_fetch
mixins:                            # fragments to include (guidance only)
  - _mixins/output-format
inputs:                            # for executable type
  - name: url
    type: string
    required: true
steps:                             # for composite type
  - skill: generation/knowledge-writer
  - skill: knowledge/knowledge-manager
---
```

## Credentials Declaration

Skills requiring API tokens or secrets declare them in frontmatter so the project secrets panel aligns with skill requirements:

```yaml
credentials:
  - service_key: kong:dev         # matches project_secrets.service_key
    environment: dev              # matches project_secrets.environment
    scope: project                # "project" = shared across project members
    required: true
    description: Kong Dev API token
  - service_key: kong:uat
    environment: uat
    scope: project
    required: false
    description: Kong UAT API token
```

Runtime: the skill's companion tool calls `get_secret(context, service_key, environment=...)`, reading `project_secrets` first, then falling back to `user_tokens`. If none found and the tool supports interactive prompts, the user is asked via a secure dialog. Plaintext never reaches the LLM — encrypted and stored server-side, then read directly by the tool's fixed code.

## Skill Types

### `guidance`
LLM-readable instructions; the agent reads the skill and follows its steps.
Use this for analysis, review, design work. No code blocks.

### `template`
Provides a code or document template for the agent to fill in and write to disk.
The body contains a `## Template` section with the template content.
Agent writes the filled template via `knowledge_rw` or `filesystem`.

### `executable`
Contains a `## Execution` section with a Python/Bash/SQL/Playwright code block.
The code block is extracted by the framework and run via the appropriate tool.
The agent fills in `inputs` values; it does NOT write the code.

```python
# Execution code block format for Playwright:
async def execute(context):
    # context.inputs["field_name"] to access inputs
    # context.browser for Playwright browser instance
    # context.db for SQL connection
    return {"result": "..."}
```

### `composite`
Chains multiple skills in sequence; the `steps` list defines the chain.
Each step's output is available to the next step via `context.prev_result`.

## Writing Good Triggers

Triggers match semantically, not by exact string match. Write 2–5 trigger phrases representing distinct ways a user might invoke this skill.

## Gap Annotations

When an agent identifies gaps in a knowledge file, it adds:
```
<!-- gaps: ["missing X", "Y not documented"] -->
```
This lowers the file's completeness score and flags it for improvement.

## Cross-Linking

Use `[[slug]]` to reference other knowledge files:
- `[[domain/context]]` — same-project domain context
- `[[system/tool-reference]]` — global tool reference
- `[[technical/api-map]]` — project technical docs

More cross-links = higher completeness score (cross_ref_density component).

## Related Knowledge

- [[system/tool-reference]]
- [[system/framework-overview]]
