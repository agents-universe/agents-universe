---
slug: "system/framework-overview"
title: "Agents Universe Framework Overview"
category: "system"
tags: ["architecture", "overview", "getting-started"]
---

# Agents Universe Framework Overview

## What This Framework Does

An enterprise AI agent framework that:
- Runs AI agents inside Linux containers
- Connects to multiple LLM providers (Azure OpenAI, OpenAI, Google Gemini, Anthropic Claude)
- Lets each user configure their own models (Settings → AI Models) and pick one per conversation
- Manages per-project knowledge with on-demand context loading
- Provides a Codex-style web UI for interacting with agents

## Core Components

### Agent (agent.py)
The orchestrator. Two modes:
- **Chat mode**: Single LLM call, streams response
- **Task mode**: Calls `plan_task` to generate a task list, then executes each task sequentially

### LLM Provider (providers/)
Abstract interface with lazy-loaded adapters: `anthropic`, `openai`, `azure_openai`, `google_gemini`.
Models are configured per user in Settings → AI Models (`user_model_configs` table, API keys
AES-256-GCM encrypted) and selected per conversation in the UI — the first usable config is the
fallback. No automatic complexity-based routing: `complexity.py` and the `model_low/mid/high`
columns on the `agents` table are legacy, unused at runtime. History compression summarizes
with the same model as the current conversation.

### Knowledge Loader (knowledge/loader.py)
Two-tier loading when a project is selected. No embedding model, no vector search.
1. **Primary files** (no `knowledge_level: detail` in frontmatter): read in full from disk,
   injected into context
2. **Detail files** (`knowledge_level: detail`): indexed in DB — only metadata + summary exposed;
   content loads on demand via `knowledge_rw load`, released when the associated task
   completes or on explicit unload
Cross-references use `[[slug]]`, resolved to `knowledge_id` at index time.

### Tool Registry (tools/)
Core tools (always available): `filesystem`, `knowledge_rw`, `memory_rw`, `web_fetch`,
`planner`, `sql_query`, `shell`.
Optional tools (lazy-loaded, enabled per agent): `browser_playwright`, `chart_renderer`,
`code_executor`, `image_annotator`, `focus_template`, `user_confirm`, `jira`, `confluence`,
`github`, `kong`, `api_request`, `test_generator`, `git_repo`.

### Skill System (skills/)
Skills are Markdown files loaded on-demand. Four types:
- `guidance` — LLM instructions
- `template` — code/content templates
- `executable` — runnable code blocks
- `composite` — chains other skills

### Workflow System (workflows/)
Same format as skills. Files end in `.workflow.md`. Agent reads and follows instructions — no separate execution engine.

## Memory Tiers (L0–L7)

| Tier | Name | Scope | Storage |
|---|---|---|---|
| L0 | In-Context | Current turn | LLM window |
| L1 | Scratchpad | Current task | In-memory |
| L2 | Session | Login session × project | Redis |
| L3 | Personal | User-scoped persistent | DB |
| L4 | Episodic | Auto-summarized sessions | DB |
| L5 | Project Knowledge | Project-shared | Markdown + DB |
| L6 | Workspace Knowledge | Workspace-shared | Markdown + DB |
| L7 | Global System | All users, read-only | Markdown |

## Project Isolation

All knowledge, conversations, memories, and scripts are strictly scoped to a project.
Switching projects clears all session state. No cross-project data leakage.

## Related Knowledge

- [[system/tool-reference]] — details on each built-in tool
- [[system/skill-authoring-guide]] — how to write skills and workflows
- [[technical/db-schema]] — full database schema
