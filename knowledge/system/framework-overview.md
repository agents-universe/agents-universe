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
2. **Detail files** (`knowledge_level: detail`, or `auto` with a `parent`): indexed in DB — only
   metadata + summary exposed; content loads on demand via `knowledge_rw load`. Loads persist
   across turns (rehydrated from knowledge_load_events each turn) until an explicit
   `knowledge_rw unload`
Cross-references use `[[slug]]`, resolved to `knowledge_id` at index time.

### Tool Registry (tools/)
Core tools (always available): `filesystem`, `knowledge_rw`, `memory_rw`, `web_fetch`,
`planner` (declared as `plan_task`), `sql_query`, `shell`, `deliver_file`.
Optional tools (lazy-loaded, enabled per agent): `browser_playwright`, `chart_renderer`,
`code_executor`, `image_annotator`, `focus_template`, `user_confirm`, `jira`, `confluence`,
`github`, `kong`, `api_request`, `secret_vault`, `test_generator`, `script_writer`,
`scheduler`, `git_repo`, `repo_graph`, `skill_source`, `delegate_agent`, `list_agents`.

### Agent Delegation (delegation.py, tools/agent_delegate.py)
An agent that lacks a capability can hand a subtask to another agent instead of telling the
user to switch: `list_agents` shows the project's roster, `delegate_agent` runs one of them as
a **nested turn** and returns its summary. The parent blocks on the tool call and then writes
the final answer itself — the subtask is not a handoff of the conversation. The delegated
agent's reply is persisted as its own message row with its own `agent_slug`, so the UI shows it
with the usual "answered by X" attribution; its stream deltas, task events, abort acks and
token updates are dropped rather than forwarded, because the parent's turn still owns those.
Bounded by `AGENT_DELEGATION_MAX_DEPTH` (default 2, `0` disables), a per-conversation lock that
serializes concurrent delegations, and `AGENT_DELEGATION_ENABLED` as a kill switch. Delegation
is a privilege-escalation surface by design — a narrow agent can borrow a wider one — so the
sub-agent's own confirmation gates still apply; `delegates_to` in an agent's frontmatter can
narrow the roster it may reach.

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
