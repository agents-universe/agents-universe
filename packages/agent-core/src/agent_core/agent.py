"""Agent orchestrator — the tool-call and agentic loop."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return f"<bytes length={len(obj)}>"
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, Path):
        return str(obj)
    # Last resort: MCP-injected tools may return arbitrary objects. Keep the
    # turn alive with a text rendering instead of letting TypeError escape the
    # tool-loop try/except (the _dumps call sits outside it).
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)

import frontmatter

from .knowledge.loader import KnowledgeContextResult
from .model_routing import resolve_tier_config
from .providers.base import LLMProvider, Message, StopReason, ToolDefinition
from .providers.registry import get_provider
from .session import ConversationSession, UserInputEntry
from .skills.registry import SkillRegistry
from .tools.base import Tool, ToolContext
from .tools.registry import build_tool_registry

_log = logging.getLogger("agent_core.agent")

# Upper bound on accumulated streamed tool-call arguments (chars) to avoid
# unbounded memory growth from a runaway stream.
_MAX_TOOL_ARGS_CHARS = 2_000_000


def _attachment_ref(a: dict) -> str:
    """Text representation of an attachment (name + relative path) for the LLM."""
    return (
        f"Attachment: {a.get('name', 'unknown')} ({a.get('media_type', '')}, "
        f"{a.get('size', 0)} bytes) — file path: {a.get('rel_path', '')}"
    )


def build_user_content(user_message: str, attachments: list[dict] | None, supports_vision: bool) -> str | list:
    """Build the user message content for the LLM.

    Without attachments the raw string is returned unchanged. With attachments
    content becomes a multimodal part list: text parts (original message,
    inline-text attachments, path references) followed by image parts — the
    convention all three provider adapters accept. Images are only sent as
    vision blocks when the provider supports it; binary files and oversized
    images degrade to path references so the agent can read them with tools.
    """
    if not attachments:
        return user_message
    parts: list[dict] = []
    if user_message:
        parts.append({"type": "text", "text": user_message})
    for a in attachments:
        if a.get("inline_text"):
            parts.append({"type": "text", "text": f"### Attachment: {a['name']}\n{a['inline_text']}"})
        elif a.get("image_data") and supports_vision:
            parts.append({"type": "image", "media_type": a["image_media_type"], "data": a["image_data"]})
        else:
            parts.append({"type": "text", "text": _attachment_ref(a)})
    return parts


@dataclass
class AgentConfig:
    slug: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    max_tokens: int = 128000
    token_budget: int = 100000

    @classmethod
    def from_file(cls, path: str) -> "AgentConfig":
        post = frontmatter.load(path)
        meta = post.metadata
        return cls(
            slug=meta["slug"],
            description=meta.get("description", ""),
            system_prompt=post.content,
            tools=meta.get("tools", []),
            skills=meta.get("skills", []),
            workflows=meta.get("workflows", []),
            knowledge=meta.get("knowledge", []),
            max_tokens=meta.get("max_tokens", 128000),
            token_budget=meta.get("token_budget", 100000),
        )


class Agent:
    """The main agent orchestrator.

    Runs in two modes:
    - Chat mode: single LLM call + tool loop
    - Task mode: plan_task → execute each task sequentially
    """

    # Bound for awaiting the API handler's persistence ack of an injected
    # user message. A timeout degrades to success (durability vs. progress).
    _INJECT_PERSIST_TIMEOUT = 30.0

    def __init__(
        self,
        config: AgentConfig,
        credentials: dict[str, dict],  # {config_id: {api_key, base_url?, ...}}
        tier_models: dict[str, dict],  # {config_id: {provider, model}}
        skill_registry: SkillRegistry,
        tool_context: ToolContext,
        project_context: KnowledgeContextResult | None = None,
        db_session: Any = None,
        pending_task_context: str = "",
        personal_memory_context: str = "",
        active_plan_context: str = "",
        workflow_registry: Any = None,
        tier_map: dict[str, str] | None = None,  # {complexity tier: config_id}, auto-route mode
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._tier_models = tier_models
        self._tier_map = tier_map
        self._skill_registry = skill_registry
        self._workflow_registry = workflow_registry
        self._tool_ctx = tool_context
        self._project_context = project_context
        self._db = db_session
        self._pending_task_context = pending_task_context
        self._personal_memory_context = personal_memory_context
        self._active_plan_context = active_plan_context
        # Filter out MCP markers (mcp / mcp:<slug>) - they are resolved into
        # dynamic tool instances by attach_mcp_tools() before run().
        static_tool_names = [t for t in config.tools if not t.startswith("mcp")]
        self._tools = build_tool_registry(static_tool_names)
        self._turn = 0
        self._current_task_id: str | None = None
        self._prompt_dirty = False
        # _prompt_dirty is a shared boolean — with parallel tasks
        # task A's change marks it, task B's next iteration consumes and
        # clears it, so A's prompt never rebuilds. _prompt_revision makes
        # each loop rebuild independently (see _mark_prompt_dirty).
        self._prompt_revision = 0
        self._static_prompt_cache: str | None = None
        self._provider_cache: dict[str, LLMProvider] = {}
        self._task_plan: list[dict] | None = None  # Live task status tracking
        for tool in self._tools.values():
            self._tool_ctx.register_tool(tool)
        # Link project context to tool context
        if project_context is not None:
            self._tool_ctx.project_context = project_context

    def add_tools(self, tools: dict[str, Tool]) -> None:
        """Inject dynamic tool instances after construction.

        Called by the WebSocket handler to attach MCP-discovered tools before
        ``run()`` snapshots ``tool_defs``.  Existing tool names take precedence
        (built-in tools are never shadowed by MCP tools).
        """
        import logging as _logging
        _log_a = _logging.getLogger("agent_core.agent")
        for name, tool in tools.items():
            if name in self._tools:
                _log_a.warning("Tool name collision, skipping dynamic tool: %s", name)
                continue
            self._tools[name] = tool
            self._tool_ctx.register_tool(tool)

    async def close(self) -> None:
        """Release provider HTTP clients and cached connections.

        Safe to call multiple times. Must be awaited after run() completes so
        per-message Agent instances don't leak httpx.AsyncClient connections.
        """
        for provider in self._provider_cache.values():
            try:
                await provider.close()
            except Exception:
                pass
        self._provider_cache.clear()

    def _get_provider(self, config_id: str) -> LLMProvider:
        """Instantiate (or retrieve cached) LLM provider for a given config_id."""
        model_cfg = self._tier_models.get(config_id)
        if not model_cfg:
            if not self._tier_models:
                raise RuntimeError(
                    "No model configured. Go to Settings → AI Models to add a provider."
                )
            config_id = next(iter(self._tier_models))
            model_cfg = self._tier_models[config_id]
        provider_type = model_cfg.get("provider", "openai")
        model_name = model_cfg.get("model", "")
        cache_key = f"{config_id}:{model_name}"
        if cache_key in self._provider_cache:
            return self._provider_cache[cache_key]
        creds = self._credentials.get(config_id, {})
        merged = {**creds, **{k: v for k, v in model_cfg.items() if k != "provider"}}
        provider = get_provider(provider_type, merged)
        self._provider_cache[cache_key] = provider
        return provider

    @staticmethod
    def _merge_tool_args(existing: str, delta: str) -> str:
        """Merge streaming tool-call argument fragments.

        OpenAI-style providers split one JSON document across chunks (plain
        string concatenation is correct); Gemini sends per-chunk incremental
        field dicts, so naive concatenation yields invalid JSON like
        ``{"a":1}{"b":2}``. Concatenation wins whenever the combined text is
        already a single valid JSON document — the dict merge is tried only
        when concatenation does not parse (so a fragment split that happens to
        land on a JSON boundary never changes the argument semantics). The
        result is capped at _MAX_TOOL_ARGS_CHARS.
        """
        if not delta:
            return existing
        if not existing:
            return delta
        combined = existing + delta
        try:
            json.loads(combined)
            result = combined
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                base = json.loads(existing)
                add = json.loads(delta)
                if isinstance(base, dict) and isinstance(add, dict):
                    merged = dict(base)
                    merged.update(add)
                    result = json.dumps(merged, ensure_ascii=False)
                else:
                    result = combined
            except (json.JSONDecodeError, TypeError, ValueError):
                result = combined
        if len(result) > _MAX_TOOL_ARGS_CHARS:
            _log.warning("Tool call arguments exceeded %d chars; truncating", _MAX_TOOL_ARGS_CHARS)
            result = result[:_MAX_TOOL_ARGS_CHARS]
        return result

    @staticmethod
    def _accumulate_tool_delta(buffer: dict, delta: dict) -> None:
        """Accumulate a streaming tool-call delta chunk into the buffer keyed by index."""
        idx = delta.get("index", 0)
        if idx not in buffer:
            buffer[idx] = {"id": delta.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
        tc = buffer[idx]
        new_id = delta.get("id", "")
        if new_id:
            tc["id"] = new_id
        fn = delta.get("function", {})
        if fn.get("name"):
            tc["function"]["name"] = fn["name"]
        tc["function"]["arguments"] = Agent._merge_tool_args(
            tc["function"].get("arguments", ""), fn.get("arguments", "")
        )

    @staticmethod
    def _drop_orphan_tool_messages(messages: list[Message]) -> list[Message]:
        """Remove tool messages whose assistant tool_calls partner is missing.

        Interrupted runs or history slicing can leave a tool result without its
        preceding assistant tool_calls message; OpenAI-compatible providers
        reject that combination with a 400. A dangling assistant tool_calls at
        the tail (its tool results never arrived) is equally rejected, so it
        is dropped too. Mid-history pairs are kept — the loop always appends a
        tool result immediately after the assistant message that requested it.
        """
        pending: set[str] = set()
        cleaned: list[Message] = []
        for message in messages:
            if message.role == "assistant":
                for call in message.tool_calls or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        pending.add(call_id)
            elif message.role == "tool":
                if message.tool_call_id not in pending:
                    _log.warning(
                        "Dropping orphan tool message (tool_call_id=%s)", message.tool_call_id
                    )
                    continue
                pending.discard(message.tool_call_id)
            cleaned.append(message)
        while cleaned and cleaned[-1].role == "assistant" and cleaned[-1].tool_calls:
            _log.warning(
                "Dropping dangling assistant tool_calls message (tool_calls=%s)",
                [str(c.get("id")) for c in cleaned[-1].tool_calls],
            )
            cleaned.pop()
        return cleaned

    @staticmethod
    def _history_tool_call_summary(messages: list[Message]) -> tuple[str, list[str]]:
        """Return a compact, secret-safe summary of tool-call history."""
        pending: set[str] = set()
        roles: list[str] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                tool_names = [
                    str(call.get("function", {}).get("name", "?"))
                    for call in message.tool_calls
                ]
                roles.append(f"assistant[{','.join(tool_names)}]")
                pending.update(
                    str(call.get("id"))
                    for call in message.tool_calls
                    if call.get("id")
                )
            elif message.role == "tool":
                roles.append(f"tool[{message.name or '?'}]")
                if message.tool_call_id:
                    pending.discard(message.tool_call_id)
            else:
                roles.append(message.role)
        return " -> ".join(roles), sorted(pending)

    @staticmethod
    def _build_task_messages(
        messages: list[Message], plan_tool_id: str, task_title: str
    ) -> list[Message]:
        """Fork a valid tool-result history for execution of one planned task.

        The outer loop cannot append its final plan summary until every task has
        finished. The nested task request must nevertheless acknowledge the
        enclosing plan_task call before adding its task-specific user message.
        """
        return messages + [
            Message(
                role="tool",
                content=json.dumps({
                    "status": "accepted",
                    "message": "Plan accepted; task execution is in progress.",
                }),
                tool_call_id=plan_tool_id,
                name="plan_task",
            ),
            Message(role="user", content=f"Execute this task: {task_title}"),
        ]

    @staticmethod
    def _normalize_task_plan(tasks: list[dict]) -> list[dict]:
        """Rewrite planner-local task IDs to globally unique IDs for persistence."""
        original_ids = [str(task.get("id") or f"task-{idx}") for idx, task in enumerate(tasks)]
        task_ids = [str(uuid.uuid4()) for _ in tasks]
        id_map: dict[str, str] = {}
        for original_id, task_id in zip(original_ids, task_ids, strict=True):
            id_map.setdefault(original_id, task_id)

        normalized: list[dict] = []
        for task, original_id, task_id in zip(tasks, original_ids, task_ids, strict=True):
            normalized_task = dict(task)
            normalized_task["id"] = task_id
            depends_on = normalized_task.get("depends_on")
            if isinstance(depends_on, list):
                normalized_task["depends_on"] = [
                    id_map.get(str(dep), str(dep)) for dep in depends_on
                ]
            normalized.append(normalized_task)

        return normalized

    @staticmethod
    def _build_dependency_graph(
        tasks: list[dict],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """Build forward and reverse dependency maps from a normalized task list.

        Returns:
            deps:       {task_id -> set(dep_ids still pending)}
            dependents: {dep_id -> set(task_ids that depend on it)}

        Unknown dep IDs (not in task list) are silently dropped.
        Raises ``ValueError`` if a dependency cycle is detected.
        """
        task_ids = {str(t["id"]) for t in tasks}
        deps: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {tid: set() for tid in task_ids}

        for t in tasks:
            tid = str(t["id"])
            raw_deps = t.get("depends_on") or []
            valid = {str(d) for d in raw_deps if str(d) in task_ids}
            deps[tid] = set(valid)
            for dep_id in valid:
                dependents[dep_id].add(tid)

        # Cycle detection via Kahn's algorithm
        in_degree = {tid: len(d) for tid, d in deps.items()}
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            current = queue.pop()
            visited += 1
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        if visited != len(task_ids):
            raise ValueError("Cycle detected in task dependencies")

        return deps, dependents

    async def run(
        self,
        user_message: str,
        history: list[Message],
        session: ConversationSession,
        provider_override: str | None = None,
        attachments: list[dict] | None = None,
        auto_tier: str | None = None,
    ) -> None:
        """Entry point for a single user turn. Emits events to session.

        attachments: prepared attachment records ({name, media_type, size,
        rel_path, inline_text?, image_data?, image_media_type?}) — the API
        handler validates them and derives file content from disk.

        auto_tier: the complexity tier ("low"/"mid"/"high") the caller's
        pre-classifier assigned to this turn in auto-route mode; surfaced on
        the model_selected event for the UI. None for explicit selection.
        """
        from .compressor import compress_history, estimate_history_tokens

        # Advance turn counter and sync to tool context
        self._turn += 1
        self._tool_ctx.current_turn = self._turn
        self._tool_ctx.current_task_id = self._current_task_id

        # 1. Select model config via override or first available
        config_id = provider_override if (provider_override and provider_override in self._tier_models) else None
        if not config_id:
            if not self._tier_models:
                raise RuntimeError("No model configured. Go to Settings → AI Models to add a provider.")
            config_id = next(iter(self._tier_models))
        model_cfg = self._tier_models.get(config_id, {})
        provider_key = model_cfg.get("provider", "openai")
        await session.emit("model_selected", provider=provider_key, model=model_cfg.get("model", ""), tier=auto_tier)

        # 2. Emit context usage info
        if self._project_context and self._project_context.loaded_entries:
            await session.emit(
                "knowledge_loaded",
                files=[e.slug for e in self._project_context.loaded_entries],
            )

        # 3. Build system prompt with project context
        system = self._build_system_prompt()

        # 4. Check if message triggers any skills (per-turn only, not persisted)
        pinned_skills = self._skill_registry.matching_triggers(user_message)
        if pinned_skills:
            for skill in pinned_skills[:3]:
                system += f"\n\n## Active Skill: {skill.slug}\n{skill.body}"

        # 5. Compress history only against the provider context window. The
        # conversation budget is a usage display value, not a runtime limit.
        _log.info("agent.run: step5 compress_history start, provider=%s", provider_key)
        try:
            import asyncio as _asyncio
            comp_provider = self._get_provider(config_id)
            tool_defs = [t.to_definition() for t in self._tools.values()]
            reserved_context = (
                estimate_history_tokens([Message(role="system", content=system), Message(role="user", content=user_message)])
                + sum(estimate_history_tokens([Message(role="system", content=f"{t.name}\n{t.description}\n{t.parameters}")]) for t in tool_defs)
                + self._config.max_tokens
            )
            available_budget = comp_provider.context_window - reserved_context
            if available_budget <= 0:
                # The reserved content (system + user + tools + max_tokens)
                # already overflows the window — compression has no room to
                # help (its summary + recent tail would also overflow) and
                # `max(budget, 1)` would force-compress on every turn with a
                # threshold of 0, failing pointlessly. Skip it; the provider's
                # context_exceeded handling takes over .
                _log.warning(
                    "agent.run: skip compression — reserved context (%d) exceeds window (%d)",
                    reserved_context, comp_provider.context_window,
                )
            else:
                async with _asyncio.timeout(30):
                    history = await compress_history(history, available_budget, comp_provider)
        except TimeoutError:
            _log.warning("History compression timed out, keeping recent messages only")
            await session.emit("warning", message="History compression timed out, keeping recent messages only")
            from .compressor import RECENT_TURNS_KEEP
            if len(history) > RECENT_TURNS_KEEP:
                system_msgs = [m for m in history if m.role == "system"]
                trimmed = history[-RECENT_TURNS_KEEP:]
                # Same pair-wise pop as compressor.split_early_recent: a tool
                # message must not lead the request, and an assistant tool_calls
                # must not dangle without its tool results (provider 400).
                while trimmed and (
                    trimmed[0].role == "tool"
                    or (trimmed[0].role == "assistant" and trimmed[0].tool_calls)
                ):
                    trimmed.pop(0)
                history = system_msgs + trimmed
        except Exception as e:
            _log.warning("History compression failed, using full history: %s", e, exc_info=True)
            await session.emit("warning", message=f"History compression failed, using full history: {e}")

        # 6. Build messages (attachments make user content a multimodal part list)
        provider = self._get_provider(config_id)
        messages = [Message(role="system", content=system)] + history + [
            Message(role="user", content=build_user_content(user_message, attachments, provider.supports_vision))
        ]

        # 7. Build tool definitions
        tool_defs = [t.to_definition() for t in self._tools.values()]

        # 8. Run the appropriate loop
        _log.info("agent.run: step8 _run_loop start, provider=%s", provider_key)
        await self._run_loop(messages, tool_defs, provider, session, config_id)
        _log.info("agent.run: step8 _run_loop done")

    def _mark_prompt_dirty(self) -> None:
        """Record that the system prompt must be rebuilt before the next turn.

        _prompt_dirty alone was a shared boolean — with parallel
        tasks, task A's knowledge change set it, task B's next iteration saw
        the flag, rebuilt B's prompt and cleared it, so A's prompt never
        refreshed (random, schedule-dependent staleness). The monotonic
        revision lets every loop rebuild independently.
        """
        self._prompt_dirty = True
        self._prompt_revision += 1

    def _build_system_prompt(self) -> str:
        """Build the full system prompt with cached static portion + dynamic parts."""
        static = self._get_static_prompt()
        dynamic = self._build_dynamic_prompt()
        return static + dynamic if dynamic else static

    def _get_static_prompt(self) -> str:
        """Return cached static portion (agent prompt + static knowledge + memory/plan/tasks)."""
        if self._static_prompt_cache is not None:
            return self._static_prompt_cache

        parts = [self._config.system_prompt]

        workspace = self._tool_ctx.project_fs_path
        parts.append(
            f"\n## Workspace Convention\n"
            f"Your workspace root is `{workspace}`. "
            "All filesystem and shell paths are relative to this root. "
            "Key directories: `knowledge/`, `tests/generated/`, `.tmp/media/`, `.tmp/work/`. "
            "Files you write to the code_executor `OUTPUT_DIR`, or hand over with the "
            "`deliver_file` tool, appear in the chat as images or downloadable "
            "attachments for the user. "
            "Never use `../`, absolute paths, or `projects/<slug>/` prefixes to access "
            "files outside this workspace - sibling project directories are invisible to you. "
            "Cross-project access is blocked by the filesystem, shell, and code_executor tools; "
            "a rejection from these tools is expected behavior, not an error to work around. "
            "If you need information from another project, ask the user."
        )

        parts.append(
            "\n## Interaction Style\n"
            "Before calling any tool, first briefly state what you are about to do and why "
            "(one sentence is enough). After a tool returns, summarize the result and state "
            "what you will do next before calling the next tool. "
            "Never call tools silently — the user should always know the current step."
        )

        parts.append(self._build_tools_prompt())

        parts.append(
            "\n## Incremental Execution Protocol\n"
            "For implementation or multi-step work, call `plan_task` before making changes. "
            "Break the goal into small, independently reviewable tasks; each task must name "
            "its expected output and a concrete verification method. Complete one task at a "
            "time: inspect the relevant context, make only that task's change, run its stated "
            "verification, and report the result before starting the next task. Do not mark a "
            "task complete when its verification failed or was skipped; record the blocker or "
            "remaining work instead. Keep task summaries factual so they can be used after "
            "history compression.\n\n"
            "When creating a Git commit, first inspect the working tree and select only the "
            "files belonging to the verified task. A commit is an externally meaningful, "
            "hard-to-reverse action: it always requires an explicit user confirmation immediately "
            "before the commit. Never commit unrelated changes or commit merely because a task "
            "is finished."
        )

        if self._project_context:
            ctx = self._project_context
            if ctx.loaded_content:
                parts.append("\n## Project Knowledge")
                for slug, content in ctx.loaded_content.items():
                    parts.append(f"\n### [[{slug}]]\n{content}")

            if ctx.overflow_slugs:
                parts.append(
                    "\n## Additional Knowledge (use knowledge_rw read)\n"
                    + "\n".join(f"- [[{s}]]" for s in ctx.overflow_slugs)
                )

        if self._personal_memory_context:
            parts.append(f"\n{self._personal_memory_context}")

        if self._active_plan_context:
            parts.append(f"\n{self._active_plan_context}")

        if self._pending_task_context:
            parts.append(f"\n{self._pending_task_context}")

        if self._config.workflows and self._workflow_registry:
            wf_parts = []
            for wf_slug in self._config.workflows:
                defn = self._workflow_registry.get(wf_slug)
                if defn:
                    wf_parts.append(f"- **{wf_slug}**: {defn.description}")
            if wf_parts:
                parts.append("\n## Available Workflows\n" + "\n".join(wf_parts))
                parts.append(
                    '\nTo follow a workflow, use `filesystem read_file` on '
                    '`workflows/<slug>.workflow.md` for detailed steps.'
                )

        self._static_prompt_cache = "\n".join(parts)
        return self._static_prompt_cache

    def _build_tools_prompt(self) -> str:
        """Build the '## Available Tools & Behaviors' section from self._tools."""
        lines = [
            "\n## Available Tools & Behaviors",
            "You have exactly the tools listed below. Do not attempt operations none of "
            "them cover (e.g. raw network access, package installation, host system "
            "changes) — they will fail.",
        ]
        for tool in self._tools.values():
            hint = tool.prompt_hint or tool.description.split(". ")[0].split("\n")[0]
            lines.append(f"- **{tool.name}** — {hint}")
        lines.append(
            "\nBehavior rules:\n"
            "- Prefer the purpose-built tool over a shell equivalent (e.g. filesystem "
            "read over `cat`).\n"
            "- A tool rejection or sandbox block is expected behavior: follow the reason "
            "given and switch tools. Never retry the same operation with rephrased "
            "commands or flags.\n"
            "- If no listed tool can do what you need, tell the user instead of improvising."
        )
        return "\n".join(lines)

    def _build_dynamic_prompt(self) -> str:
        """Build the dynamic portion (task plan + dynamically loaded knowledge + deferred table)."""
        parts: list[str] = []

        # Live task plan (updated as tasks complete)
        if self._task_plan:
            status_icon = {"completed": "✓", "running": "→", "failed": "✗", "skipped": "-"}
            parts.append("\n## Current Task Plan")
            for t in self._task_plan:
                icon = status_icon.get(t.get("status", "pending"), " ")
                title = t.get("title", "")
                status = t.get("status", "pending")
                suffix = f" ({status})" if status not in ("pending", "completed") else ""
                parts.append(f"[{icon}] {title}{suffix}")

        if self._project_context:
            ctx = self._project_context

            if ctx.dynamically_loaded:
                parts.append("\n## Dynamically Loaded Knowledge")
                parts.append("These will be released when the associated task completes, or use `knowledge_rw unload`.\n")
                for slug, content in ctx.dynamically_loaded.items():
                    record = ctx.dynamic_records.get(slug)
                    task_info = f"(task: {record.task_id})" if record and record.task_id else "(manual)"
                    parts.append(f"\n### [[{slug}]] {task_info}\n{content}")

            if ctx.deferred_entries:
                parts.append("\n## Available Detail Knowledge")
                parts.append('Use `knowledge_rw load slug="..."` to load into context.\n')
                parts.append("| Slug | Summary | Parent |")
                parts.append("|------|---------|--------|")
                for slug, entry in ctx.deferred_entries.items():
                    parts.append(f"| `{slug}` | {entry.summary} | {entry.parent_slug or '-'} |")

        return "\n".join(parts) if parts else ""

    def _invalidate_static_cache(self) -> None:
        """Call when static knowledge changes (write to static file)."""
        self._static_prompt_cache = None

    async def _consume_injected_input(
        self,
        session: ConversationSession,
        messages: list[Message],
        provider: LLMProvider,
        message_id: str,
    ) -> str | None:
        """Consume every queued user message at a step boundary.

        The current step's partial output is finalized as an "interrupted"
        snapshot, the user messages are persisted by the API handler
        (user_message_injected events), and the loop continues with the new
        instructions appended to the message history. Returns the new message
        id, or None when nothing was queued (or every message failed
        validation — the handler already notified the client).
        """
        entries: list[UserInputEntry] = []
        while True:
            entry = session.dequeue_user_input()
            if entry is None:
                break
            entries.append(entry)
        if not entries:
            return None

        # Snapshot the current partial output as an interrupted message
        # (persisted by forward_events). emitted_end stays False — the turn
        # continues and must not be double-finalized by the finally block.
        await session.emit(
            "stream_end",
            message_id=message_id,
            total_tokens=session.tokens_used,
            stop_reason="interrupted",
        )

        # Ask the handler to persist the user messages now — event order
        # guarantees they land after the interrupted snapshot above.
        for entry in entries:
            await session.emit(
                "user_message_injected",
                message_id=entry.message_id,
                content=entry.content,
                attachments=entry.attachments,
                agent_id=entry.agent_id,
                config_id=entry.config_id,
            )

        async def _await_persist(entry: UserInputEntry) -> bool:
            if entry.persisted is None:
                return True
            try:
                return await asyncio.wait_for(
                    asyncio.shield(entry.persisted), timeout=self._INJECT_PERSIST_TIMEOUT
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # Timeout only gates durability, not agent progress — degrade
                # to success so a stuck handler never stalls the loop.
                return True

        oks = await asyncio.gather(*[_await_persist(e) for e in entries])
        accepted = [e for e, ok in zip(entries, oks) if ok]
        for entry in accepted:
            messages.append(Message(
                role="user",
                content=build_user_content(
                    entry.content, entry.attachment_records or [], provider.supports_vision
                ),
            ))
            entry.consumed = True
        if not accepted:
            return None
        return session.new_message()

    async def _run_loop(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        provider: LLMProvider,
        session: ConversationSession,
        config_id: str,
    ) -> None:
        """Tool-call loop with stop_reason state machine."""
        # Defense in depth: resumed history (interrupted runs, compressed
        # slices) can contain tool messages without their assistant partner;
        # sanitize once here so every provider call in this turn is well-formed.
        messages = self._drop_orphan_tool_messages(messages)
        message_id = session.new_message()
        max_iterations = 20
        max_pause_continuations = 3
        pause_count = 0
        emitted_end = False
        # per-loop prompt revision — each loop tracks its own last
        # build; a change by ANY parallel task bumps the shared revision.
        prompt_revision = self._prompt_revision

        try:
            budget = max_iterations
            # Safety valve: injections refresh budget but never total_budget,
            # so a pathological stream of injections cannot loop forever.
            total_budget = max_iterations * 5

            async def _maybe_inject() -> bool:
                """Consume queued user input at a step boundary and refresh
                the iteration budget — an injection is a new user instruction."""
                nonlocal message_id, budget
                if session.is_aborted():
                    return False
                new_id = await self._consume_injected_input(
                    session, messages, provider, message_id
                )
                if new_id is None:
                    return False
                message_id = new_id
                budget = max_iterations
                return True

            exhausted = False
            while True:
                if session.is_aborted():
                    await session.emit("abort_ack", message_id=message_id)
                    # Persist whatever accumulated so an interrupted run stays
                    # visible in history after a reload — the hard-cancel path
                    # already finalizes via the finally below; the graceful
                    # abort must too, or the partial message vanishes.
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="aborted")
                    emitted_end = True
                    break

                if budget <= 0 or total_budget <= 0:
                    # Budget exhausted. An injection that arrived since the
                    # last check (e.g. while a tool was executing) refreshes
                    # the budget; otherwise the loop ends. total_budget is the
                    # hard safety valve and is never refreshed.
                    if budget <= 0 and await _maybe_inject():
                        continue
                    exhausted = True
                    break

                budget -= 1
                total_budget -= 1
                iteration = max_iterations - budget

                if await _maybe_inject():
                    continue

                if self._prompt_revision != prompt_revision:
                    messages[0] = Message(role="system", content=self._build_system_prompt())
                    prompt_revision = self._prompt_revision

                # Stream the response
                full_text = ""
                tool_calls_buffer: dict[int, dict] = {}
                preparing_emitted: set[int] = set()
                stop_reason: StopReason = StopReason.UNKNOWN

                history_summary, pending_tool_ids = self._history_tool_call_summary(messages)
                _log.info(
                    "_run_loop iter=%d provider=%s messages=%d history=%s pending_tool_call_ids=%s: calling provider.stream",
                    iteration,
                    provider.model_name,
                    len(messages),
                    history_summary,
                    pending_tool_ids,
                )
                try:
                    async for chunk in provider.stream(
                        messages, tool_defs, max_tokens=self._config.max_tokens
                    ):
                        if chunk.delta:
                            full_text += chunk.delta
                            await session.emit("stream_delta", delta=chunk.delta, message_id=message_id)

                        if chunk.tool_call_delta:
                            idx = chunk.tool_call_delta.get("index", 0)
                            is_new = idx not in tool_calls_buffer
                            if is_new:
                                tool_calls_buffer[idx] = {"id": chunk.tool_call_delta.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
                            tc = tool_calls_buffer[idx]
                            new_id = chunk.tool_call_delta.get("id", "")
                            if new_id:
                                tc["id"] = new_id
                            fn = chunk.tool_call_delta.get("function", {})
                            if fn.get("name"):
                                tc["function"]["name"] = fn["name"]
                            if idx not in preparing_emitted and tc["id"] and tc["function"]["name"]:
                                preparing_emitted.add(idx)
                                await session.emit("tool_call_preparing", tool=tc["function"]["name"], call_id=tc["id"])
                            current_args = tc["function"].get("arguments", "")
                            tc["function"]["arguments"] = self._merge_tool_args(current_args, fn.get("arguments", ""))

                        if chunk.stop_reason:
                            stop_reason = chunk.stop_reason

                        if chunk.usage:
                            session.add_usage(
                                prompt_tokens=chunk.usage.get("prompt_tokens", 0),
                                completion_tokens=chunk.usage.get("completion_tokens", 0),
                            )
                            await session.emit("token_update", used=session.tokens_used, budget=session.token_budget)
                except Exception as api_err:
                    err_type = type(api_err).__name__
                    err_msg = (
                        f"LLM API error: {api_err}\n"
                        f"  model={provider.model_name}, iteration={iteration}, "
                        f"messages={len(messages)}, error_type={err_type}"
                    )
                    if pending_tool_ids:
                        err_msg += f", pending_tool_call_ids={pending_tool_ids}"
                    _log.error(
                        "provider.stream() failed: provider=%s iter=%d messages=%d pending_tool_call_ids=%s error=%s",
                        provider.model_name,
                        iteration,
                        len(messages),
                        pending_tool_ids,
                        api_err,
                        exc_info=True,
                    )
                    await session.emit("error", message=err_msg)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="api_error")
                    emitted_end = True
                    break

                # Append assistant message to history. A stream truncated
                # mid-tool-call (finish_reason="length", pause_turn, dropped
                # connection) can leave entries whose id/name never arrived —
                # persisting them would orphan a tool_call without a matching
                # tool_result and the next provider request 400s on it. Drop
                # incomplete entries here (MAX_TOKENS/END_TURN then fall
                # through to the plain-text path below).
                tool_calls = (
                    [tc for tc in tool_calls_buffer.values()
                     if tc.get("id") and tc.get("function", {}).get("name")]
                    if tool_calls_buffer else None
                ) or None
                assistant_msg = Message(role="assistant", content=full_text, tool_calls=tool_calls)
                messages.append(assistant_msg)

                # ─── Stop Reason State Machine ────────────────────────────
                if stop_reason == StopReason.REFUSAL:
                    await session.emit("refusal", message_id=message_id, content=full_text)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="refusal")
                    emitted_end = True
                    break

                if stop_reason == StopReason.CONTEXT_EXCEEDED:
                    await session.emit("context_exceeded", message_id=message_id)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="context_exceeded")
                    emitted_end = True
                    break

                if stop_reason == StopReason.CONTENT_FILTER:
                    await session.emit("content_filtered", message_id=message_id)
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="content_filter")
                    emitted_end = True
                    break

                if stop_reason == StopReason.MAX_TOKENS:
                    if not tool_calls:
                        if await _maybe_inject():
                            continue
                        await session.emit("warning", message="Response truncated (max_tokens reached)")
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="max_tokens")
                        emitted_end = True
                        break

                if stop_reason == StopReason.PAUSE_TURN:
                    pause_count += 1
                    if pause_count > max_pause_continuations:
                        if await _maybe_inject():
                            continue
                        await session.emit("warning", message="Too many pause_turn continuations, stopping")
                        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="pause_turn_exhausted")
                        emitted_end = True
                        break
                    # Continue without tool processing — the model paused mid-generation
                    if not tool_calls:
                        if await _maybe_inject():
                            continue
                        continue

                if stop_reason in (StopReason.END_TURN, StopReason.UNKNOWN) and not tool_calls:
                    if await _maybe_inject():
                        continue
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used)
                    emitted_end = True
                    break

                if not tool_calls:
                    if await _maybe_inject():
                        continue
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used)
                    emitted_end = True
                    break

                # ─── Process Tool Calls ───────────────────────────────────
                pause_count = 0  # Reset on successful tool_use cycle

                # Complete ordinary calls before entering task mode. A nested task
                # request inherits this assistant turn, so every sibling call must
                # already have a matching tool response in its history.
                for tc in sorted(
                    tool_calls,
                    key=lambda item: item.get("function", {}).get("name", "") == "plan_task",
                ):
                    tool_name = tc.get("function", {}).get("name", "")
                    tool_id = tc.get("id")
                    if not tool_name or not tool_id:
                        continue
                    try:
                        args = json.loads(tc["function"].get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError) as _e:
                        # Keep the assistant tool call serializable for the next
                        # provider request; Anthropic reparses this field.
                        tc["function"]["arguments"] = "{}"
                        bad_result = {"error": f"Malformed tool arguments (invalid JSON): {_e}"}
                        await session.emit("tool_call_start", tool=tool_name, input={}, call_id=tool_id)
                        await session.emit("tool_call_end", tool=tool_name, output=bad_result, call_id=tool_id)
                        messages.append(Message(role="tool", content=_dumps(bad_result), tool_call_id=tool_id, name=tool_name))
                        continue

                    await session.emit("tool_call_start", tool=tool_name, input=args, call_id=tool_id)

                    if tool_name == "plan_task":
                        try:
                            task_summary = await self._run_task_mode(
                                args, provider, session, messages, tool_defs, config_id, tool_id
                            )
                            result = {"status": "completed", "summary": task_summary}
                        except asyncio.CancelledError:
                            result = {"error": "Tool execution interrupted"}
                            await session.emit("tool_call_end", tool=tool_name, output=result, call_id=tool_id, status="error")
                            raise
                        except Exception as e:
                            # Same degradation as the ordinary-tool path below:
                            # a malformed plan (tasks not a list, entries without
                            # id/title) must surface as a tool error the model
                            # can see and self-correct, not crash the whole turn
                            # into a generic "Agent execution failed".
                            _log.warning("plan_task failed (call_id=%s): %s", tool_id, e, exc_info=True)
                            # _run_task_mode may have died after arming
                            # _current_task_id / _task_plan — reset them so the
                            # stale task id is not re-injected into the shared
                            # tool context next turn (dynamic knowledge would
                            # bind to a task that never completes and leak) and
                            # the snapshot below does not render stuck tasks.
                            self._current_task_id = None
                            self._tool_ctx.current_task_id = None
                            for tp in self._task_plan or []:
                                if tp["status"] in ("pending", "running"):
                                    tp["status"] = "failed"
                                    tp["error"] = "plan_task failed"
                            result = {"error": f"plan_task failed: {str(e)[:500]}"}
                        # Attach the final plan snapshot to the tool output so
                        # finished messages can render the plan in history. The
                        # LLM-facing result below stays lean (no context bloat).
                        plan_snapshot = [
                            {
                                "id": t["id"],
                                "title": t["title"],
                                "status": t["status"],
                                **({"summary": t["summary"]} if t.get("summary") else {}),
                                **({"error": t["error"]} if t.get("error") else {}),
                            }
                            for t in getattr(self, "_task_plan", None) or []
                        ]
                        await session.emit(
                            "tool_call_end",
                            tool=tool_name,
                            output={**result, "tasks": plan_snapshot},
                            call_id=tool_id,
                        )
                        messages.append(Message(
                            role="tool",
                            content=_dumps(result),
                            tool_call_id=tool_id,
                            name=tool_name,
                        ))
                        continue

                    tool = self._tools.get(tool_name)
                    if tool is None:
                        result = {"error": f"Unknown tool: {tool_name}"}
                    else:
                        try:
                            result = await tool.execute(args, self._tool_ctx)
                        except asyncio.CancelledError:
                            result = {"error": "Tool execution interrupted"}
                            await session.emit("tool_call_end", tool=tool_name, output=result, call_id=tool_id, status="error")
                            raise
                        except Exception as e:
                            _log.warning("Tool %s failed (call_id=%s): %s", tool_name, tool_id, e, exc_info=True)
                            result = {"error": str(e)[:500]}

                    await session.emit("tool_call_end", tool=tool_name, output=result, call_id=tool_id)

                    if result.get("images"):
                        await session.emit("image_output", message_id=message_id, images=result["images"])

                    if result.get("files"):
                        _deliverable = [f for f in result["files"] if isinstance(f, dict) and f.get("url")]
                        if _deliverable:
                            await session.emit("file_output", message_id=message_id, files=_deliverable)

                    if tool_name == "knowledge_rw" and self._project_context:
                        op = args.get("operation")
                        if op == "write" and not result.get("error"):
                            from .knowledge.loader import update_context_file, _is_log_role
                            slug = args.get("slug", "")
                            content = args.get("content", "")
                            if slug and content and not _is_log_role(content):
                                update_context_file(self._project_context, slug, content)
                                self._invalidate_static_cache()
                                self._mark_prompt_dirty()
                                await session.emit("knowledge_updated")
                        elif op in ("load", "unload", "refresh", "delete", "purge"):
                            # delete/purge remove files or index rows — the
                            # prompt's knowledge listing must be rebuilt or the
                            # agent keeps reasoning about deleted files.
                            self._mark_prompt_dirty()

                    if tool_name == "memory_rw":
                        op = args.get("operation")
                        if op in ("save", "update", "archive"):
                            self._invalidate_static_cache()
                            self._mark_prompt_dirty()

                    messages.append(Message(
                        role="tool",
                        content=_dumps(result),
                        tool_call_id=tool_id,
                        name=tool_name,
                    ))
            if exhausted:
                if not emitted_end:
                    await session.emit("warning", message="Reached maximum loop iterations")
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, stop_reason="max_iterations")
                    emitted_end = True
        finally:
            if not emitted_end:
                await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used)
            await self._tool_ctx.cleanup()
            await session.close()

    def _compute_task_progress(self, current_task_id: str | None = None) -> dict[str, Any]:
        """Derive progress info from the live task plan (parallel-aware)."""
        if not self._task_plan:
            return {}
        completed = sum(1 for t in self._task_plan if t["status"] == "completed")
        total = len(self._task_plan)

        running_tasks = [t for t in self._task_plan if t["status"] == "running"]
        if len(running_tasks) == 1:
            current_title: str | None = running_tasks[0]["title"]
        elif len(running_tasks) > 1:
            current_title = f"{len(running_tasks)} 个任务并行运行中"
        else:
            current_title = None

        # Next: first pending task whose deps are all completed
        completed_ids = {t["id"] for t in self._task_plan if t["status"] == "completed"}
        next_title: str | None = None
        for t in self._task_plan:
            if t["status"] == "pending":
                dep_ids = set(t.get("depends_on", []))
                if dep_ids <= completed_ids:
                    next_title = t["title"]
                    break

        return {
            "current_step": current_title,
            "next_step": next_title,
            "progress_completed": completed,
            "progress_total": total,
        }

    async def _emit_task_progress(self, session: ConversationSession, task_id: str) -> None:
        """Emit a task_progress event with current plan state."""
        progress = self._compute_task_progress(task_id)
        if progress:
            await session.emit("task_progress", task_id=task_id, **progress)

    async def _emit_task_stream_end(
        self,
        session: ConversationSession,
        task_tool_ctx: ToolContext,
        task_id: str,
    ) -> None:
        """Close the task's stream on the frontend after a failure/abort path.

        _run_task_loop emits stream_end on its normal return paths, but
        timeout / API-error / refusal bypass them — and the frontend
        only stops the per-task typing indicator on stream_end, not on
        task_failed. When the failure happened before _run_task_loop opened
        its stream (no message_id yet), nothing was ever started — emit
        nothing instead of a stream_end with message_id=None.
        """
        message_id = getattr(task_tool_ctx, "current_message_id", None)
        if message_id is None:
            return
        try:
            await session.emit(
                "stream_end",
                message_id=message_id,
                total_tokens=session.tokens_used,
                task_id=task_id,
            )
        except Exception:
            _log.debug("stream_end emit failed after task failure", exc_info=True)

    async def _run_task_mode(
        self,
        plan_args: dict,
        provider: LLMProvider,
        session: ConversationSession,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        config_id: str,
        plan_tool_id: str,
    ) -> str:
        """Execute a planned task list with DAG-aware parallel scheduling.

        Tasks with no dependencies run concurrently (bounded by a semaphore).
        When a task completes, its dependents are checked and dispatched if
        ready. Failed tasks cause their dependents to be skipped.

        Returns summary string.
        """
        tasks = self._normalize_task_plan(plan_args.get("tasks", []))
        await session.emit("task_plan_created", tasks=tasks)

        # Initialize live task plan for dynamic prompt
        self._task_plan = [
            {
                "id": t["id"],
                "title": t["title"],
                "status": "pending",
                "depends_on": t.get("depends_on", []),
            }
            for t in tasks
        ]
        self._mark_prompt_dirty()

        if not tasks:
            summary = "No tasks to execute."
            await session.emit("agentic_loop_completed", tasks_done=0, tasks_failed=0, summary=summary)
            return summary

        # Build DAG; fall back to sequential on cycle
        try:
            deps, dependents = self._build_dependency_graph(tasks)
        except ValueError:
            _log.warning("Task dependency cycle detected, falling back to sequential execution")
            await session.emit("warning", message="Task dependency cycle detected, falling back to sequential execution")
            # Sequential fallback: chain all tasks in order
            deps = {}
            dependents = {str(t["id"]): set() for t in tasks}
            task_id_list = [str(t["id"]) for t in tasks]
            for i in range(len(task_id_list) - 1):
                deps.setdefault(task_id_list[i + 1], set()).add(task_id_list[i])
                dependents[task_id_list[i]].add(task_id_list[i + 1])

        task_map = {str(t["id"]): t for t in tasks}
        completed: set[str] = set()
        failed: set[str] = set()
        task_results: dict[str, str] = {}  # task_id -> summary text
        semaphore = asyncio.Semaphore(3)  # max concurrency

        # Scheduling state
        dispatched: set[str] = set()  # tasks already started or finished
        pending_futures: set[asyncio.Task] = set()

        async def _cascade_skip(task_id: str) -> None:
            """Mark all transitive dependents of *task_id* as skipped."""
            for dep_tid in dependents.get(task_id, set()):
                if dep_tid in completed or dep_tid in failed or dep_tid in dispatched:
                    continue
                failed.add(dep_tid)
                dispatched.add(dep_tid)
                for tp in self._task_plan:
                    if tp["id"] == dep_tid:
                        tp["status"] = "skipped"
                        tp["error"] = "Skipped: dependency failed"
                        break
                self._mark_prompt_dirty()
                await session.emit("task_skipped", task_id=dep_tid, error="Skipped: dependency failed")
                task_results[dep_tid] = f"[SKIPPED] {task_map[dep_tid]['title']}"
                await _cascade_skip(dep_tid)

        async def _dispatch_ready() -> None:
            """Start all tasks whose dependencies are fully completed."""
            for t in tasks:
                tid = str(t["id"])
                if tid in dispatched:
                    continue
                # An injection (or abort) stops new dispatch; in-flight tasks
                # finish naturally and the loop below exits.
                if session.is_aborted() or session.has_pending_user_input():
                    break
                task_deps = deps.get(tid, set())
                if not task_deps or task_deps <= completed:
                    dispatched.add(tid)
                    fut = asyncio.create_task(_execute_and_finish(tid))
                    pending_futures.add(fut)
                    # Remove self-reference when done to avoid retaining large closures
                    fut.add_done_callback(pending_futures.discard)

        async def _execute_and_finish(task_id: str) -> None:
            """Execute one task, then dispatch newly-ready dependents."""
            async with semaphore:
                if session.is_aborted():
                    # Mark as skipped if not yet started
                    failed.add(task_id)
                    for tp in self._task_plan:
                        if tp["id"] == task_id:
                            tp["status"] = "skipped"
                            tp["error"] = "Aborted"
                            break
                    self._mark_prompt_dirty()
                    await session.emit("task_skipped", task_id=task_id, error="Aborted")
                    return
                try:
                    result = await self._execute_single_task(
                        task_map[task_id], provider, session, messages,
                        tool_defs, config_id, plan_tool_id,
                    )
                except Exception as e:
                    # A crash must not leave the task stuck at "running" with
                    # its dependents forever undispatchable (gather with
                    # return_exceptions=True would silently swallow it).
                    _log.error("Task %s crashed: %s", task_id, e, exc_info=True)
                    for tp in self._task_plan:
                        if tp["id"] == task_id:
                            tp["status"] = "failed"
                            tp["error"] = f"Execution error: {e}"[:500]
                            break
                    self._mark_prompt_dirty()
                    await session.emit("task_failed", task_id=task_id, error=f"Execution error: {e}"[:500])
                    failed.add(task_id)
                    task_results[task_id] = f"[FAILED] {task_map[task_id]['title']}"
                    await _cascade_skip(task_id)
                    if not session.is_aborted():
                        await _dispatch_ready()
                    return

            status = result["status"]
            task_results[task_id] = result["summary"]

            if status == "completed":
                completed.add(task_id)
            else:
                failed.add(task_id)

            # Check dependents
            if status != "completed":
                # Failed -> cascade skip dependents
                await _cascade_skip(task_id)

            # Dispatch newly-ready tasks (unless aborted)
            if not session.is_aborted():
                await _dispatch_ready()

        # ── Initial dispatch ──────────────────────────────────────
        await _dispatch_ready()

        # ── Wait for all tasks to finish ──────────────────────────
        # Tasks are dispatched dynamically (each completion can schedule its
        # dependents), so we must loop: a single gather() snapshots the set at
        # call time and would return while newly-dispatched tasks still run.
        while pending_futures:
            await asyncio.gather(*pending_futures, return_exceptions=True)

        # In-flight injection: the user queued a message while tasks were
        # running. Stop here (remaining tasks stay pending — the handler's
        # stale-task sweep skips their ids), keep the plan in the dynamic
        # prompt, and hand control back to _run_loop, whose step boundary
        # consumes the queued message. Deferred tasks are NOT marked skipped:
        # the user may resume them with the new instruction.
        if session.has_pending_user_input():
            deferred = [
                str(t["id"]) for t in tasks
                if str(t["id"]) not in completed and str(t["id"]) not in failed
            ]
            summary = (
                f"Completed {len(completed)}/{len(tasks)} tasks. "
                "User interrupted; remaining tasks kept pending."
            )
            await session.emit(
                "agentic_loop_completed",
                tasks_done=len(completed),
                tasks_failed=len(failed),
                summary=summary[:500],
                interrupted=True,
                deferred_task_ids=deferred,
            )
            self._current_task_id = None
            self._tool_ctx.current_task_id = None
            return summary

        # Abort: mark any remaining pending tasks as skipped
        if session.is_aborted():
            for t in tasks:
                tid = str(t["id"])
                if tid not in completed and tid not in failed:
                    failed.add(tid)
                    for tp in self._task_plan:
                        if tp["id"] == tid:
                            tp["status"] = "skipped"
                            tp["error"] = "Aborted"
                            break
                    await session.emit("abort_ack", task_id=tid)
                    await session.emit("task_skipped", task_id=tid, error="Aborted")
                    task_results.setdefault(tid, f"[ABORTED] {t['title']}")

        self._current_task_id = None
        self._tool_ctx.current_task_id = None

        tasks_done = len(completed)
        tasks_failed_count = len(failed)
        summaries = [task_results.get(str(t["id"]), f"[?] {t['title']}") for t in tasks]
        summary = f"Completed {tasks_done}/{len(tasks)} tasks.\n" + "\n".join(summaries)
        await session.emit("agentic_loop_completed", tasks_done=tasks_done, tasks_failed=tasks_failed_count, summary=summary[:500])
        return summary

    async def _execute_single_task(
        self,
        task: dict,
        provider: LLMProvider,
        session: ConversationSession,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        config_id: str,
        plan_tool_id: str,
    ) -> dict:
        """Execute one planned task. Returns {task_id, status, summary}."""
        from .knowledge.loader import unload_by_task

        task_id = task["id"]
        self._turn += 1
        turn = self._turn
        task_tool_ctx = self._tool_ctx.copy_for_task(task_id, turn)
        # Auto-route mode: pick the config serving the task's complexity tier
        # (nearest-tier fallback). Explicit mode keeps the session config.
        task_config = config_id
        if self._tier_map:
            resolved = resolve_tier_config(self._tier_map, task.get("estimated_complexity"))
            if resolved and resolved in self._tier_models:
                task_config = resolved
        task_provider = self._get_provider(task_config)

        # Update live plan status
        for tp in self._task_plan:
            if tp["id"] == task_id:
                tp["status"] = "running"
                break
        self._mark_prompt_dirty()

        progress = self._compute_task_progress(task_id)
        await session.emit(
            "task_started",
            task_id=task_id,
            title=task["title"],
            model_tier=task_config,
            actual_model=task_provider.model_name,
            **progress,
        )

        task_messages = self._build_task_messages(messages, plan_tool_id, task["title"])
        task_history, pending_task_tool_ids = self._history_tool_call_summary(task_messages)
        _log.info(
            "task execution starting: task_id=%s plan_tool_id=%s provider=%s messages=%d history=%s pending_tool_call_ids=%s",
            task_id,
            plan_tool_id,
            task_provider.model_name,
            len(task_messages),
            task_history,
            pending_task_tool_ids,
        )

        async def _close_task_db() -> None:
            """Release the per-task DB session copy_for_task created (if any).

            The shared tool_db owned by the handler is closed there; per-task
            sessions must be closed here so concurrent tasks don't leak
            connection-pool slots.
            """
            _sess = task_tool_ctx.db_session
            if _sess is not None and _sess is not self._tool_ctx.db_session:
                try:
                    await _sess.close()
                except Exception:
                    _log.debug("Failed to close per-task db session", exc_info=True)

        try:
            async with asyncio.timeout(300):
                full_text = await self._run_task_loop(
                    task_messages, tool_defs, task_provider, session,
                    task_id=task_id, turn=turn, task_tool_ctx=task_tool_ctx,
                )
        except TimeoutError:
            error_text = "Task timed out (5 min)"
            await session.emit("task_failed", task_id=task_id, error=error_text)
            await self._emit_task_stream_end(session, task_tool_ctx, task_id)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "failed"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "failed", "summary": f"[TIMEOUT] {task['title']}"}
        except Exception as e:
            error_text = str(e)[:500]
            await session.emit("task_failed", task_id=task_id, error=error_text)
            await self._emit_task_stream_end(session, task_tool_ctx, task_id)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "failed"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "failed", "summary": f"[ERROR] {task['title']}: {e}"}
        finally:
            await _close_task_db()

        # An abort that fired mid-loop must not report the in-flight task as
        # completed — _run_task_mode's abort block marks the remaining pending
        # tasks "skipped", so keep the same semantics here. Otherwise this
        # task's completion event gets persisted and its result shows as done.
        # No stream_end here: _run_task_loop emits it on its normal return
        # paths (the abort break included) — a second emit with the same
        # message_id would finalize the frontend stream twice.
        if session.is_aborted():
            error_text = "Aborted"
            await session.emit("task_skipped", task_id=task_id, error=error_text)
            for tp in self._task_plan:
                if tp["id"] == task_id:
                    tp["status"] = "skipped"
                    tp["error"] = error_text
                    break
            self._mark_prompt_dirty()
            await self._emit_task_progress(session, task_id)
            if self._project_context:
                unloaded = unload_by_task(self._project_context, task_id)
                if unloaded:
                    await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                    self._mark_prompt_dirty()
            return {"task_id": task_id, "status": "skipped", "summary": f"[ABORTED] {task['title']}"}

        # Success
        await session.emit("task_completed", task_id=task_id, summary=full_text[:500])
        for tp in self._task_plan:
            if tp["id"] == task_id:
                tp["status"] = "completed"
                tp["summary"] = full_text[:500]
                break
        self._mark_prompt_dirty()

        # Emit updated progress after task status change
        await self._emit_task_progress(session, task_id)

        # Release dynamic knowledge bound to this task
        if self._project_context:
            unloaded = unload_by_task(self._project_context, task_id)
            if unloaded:
                await session.emit("knowledge_dynamic_unload", slugs=unloaded, reason="task_end")
                self._mark_prompt_dirty()

        return {"task_id": task_id, "status": "completed", "summary": f"[DONE] {task['title']}: {full_text[:200]}"}

    async def _run_task_loop(
        self,
        messages: list[Message],
        tool_defs: list[ToolDefinition],
        provider: LLMProvider,
        session: ConversationSession,
        *,
        task_id: str,
        turn: int,
        task_tool_ctx: ToolContext,
    ) -> str:
        """Tool-call loop for a single task with stop_reason handling. Returns final text.

        Uses *task_tool_ctx* (a per-task shallow copy) so concurrent tasks
        don't clobber each other's ``current_task_id`` / ``current_turn``.
        """
        max_iterations = 15
        max_pause_continuations = 3
        pause_count = 0
        message_id = session.new_message()
        # failure paths below _run_task_loop (timeout, API error,
        # refusal) must still close the stream on the frontend — task_failed
        # alone leaves the typing indicator stuck. Expose the id on the
        # per-task context so _run_task can emit stream_end from its except
        # branches (task_tool_ctx is a per-task shallow copy; no cross-task
        # clobbering).
        task_tool_ctx.current_message_id = message_id
        full_text = ""
        # Text produced in EARLIER iterations of this task loop — each
        # iteration resets full_text, but the task summary (task_completed)
        # must reflect the whole task, not just the final turn.
        full_text_total: list[str] = []
        # per-loop prompt revision (see _run_loop) — parallel tasks
        # each rebuild their own system prompt on the shared revision bump.
        prompt_revision = self._prompt_revision

        for _ in range(max_iterations):
            if session.is_aborted():
                break

            if self._prompt_revision != prompt_revision:
                messages[0] = Message(role="system", content=self._build_system_prompt())
                prompt_revision = self._prompt_revision

            full_text = ""
            tool_calls_buffer: dict[int, dict] = {}
            stop_reason: StopReason = StopReason.UNKNOWN

            task_history, pending_tool_ids = self._history_tool_call_summary(messages)
            _log.info(
                "_run_task_loop task_id=%s provider=%s messages=%d history=%s pending_tool_call_ids=%s: calling provider.stream",
                task_id,
                provider.model_name,
                len(messages),
                task_history,
                pending_tool_ids,
            )
            try:
                async for chunk in provider.stream(
                    messages, tool_defs, max_tokens=self._config.max_tokens
                ):
                    if chunk.delta:
                        full_text += chunk.delta
                        await session.emit("stream_delta", delta=chunk.delta, message_id=message_id, task_id=task_id)
                    if chunk.tool_call_delta:
                        idx = chunk.tool_call_delta.get("index", 0)
                        prev_name = tool_calls_buffer.get(idx, {}).get("function", {}).get("name", "")
                        self._accumulate_tool_delta(tool_calls_buffer, chunk.tool_call_delta)
                        tc = tool_calls_buffer[idx]
                        new_name = tc["function"]["name"]
                        if new_name and not prev_name and tc["id"]:
                            await session.emit(
                                "tool_call_preparing",
                                tool=new_name,
                                call_id=tc["id"],
                                task_id=task_id,
                            )
                    if chunk.stop_reason:
                        stop_reason = chunk.stop_reason
                    if chunk.usage:
                        session.add_usage(
                                prompt_tokens=chunk.usage.get("prompt_tokens", 0),
                                completion_tokens=chunk.usage.get("completion_tokens", 0),
                            )
                        await session.emit("token_update", used=session.tokens_used, budget=session.token_budget)
            except Exception as api_err:
                _log.error(
                    "task provider.stream() failed: task_id=%s provider=%s messages=%d pending_tool_call_ids=%s error=%s",
                    task_id,
                    provider.model_name,
                    len(messages),
                    pending_tool_ids,
                    api_err,
                    exc_info=True,
                )
                raise RuntimeError(
                    f"LLM API error: {api_err}\n"
                    f"  task_id={task_id}, model={provider.model_name}, "
                    f"messages={len(messages)}, error_type={type(api_err).__name__}"
                ) from api_err

            # Same truncation guard as the main loop: a task turn cut off
            # mid-tool-call must not orphan an incomplete tool_call in
            # history (next iteration would 400 on the message sequence).
            tool_calls = (
                [tc for tc in tool_calls_buffer.values()
                 if tc.get("id") and tc.get("function", {}).get("name")]
                if tool_calls_buffer else None
            ) or None
            messages.append(Message(role="assistant", content=full_text, tool_calls=tool_calls))

            # ─── Stop Reason Handling ─────────────────────────────────
            if stop_reason == StopReason.REFUSAL:
                raise RuntimeError(f"Model refused: {full_text[:200]}")

            if stop_reason == StopReason.CONTEXT_EXCEEDED:
                raise RuntimeError("Context window exceeded during task execution")

            if stop_reason == StopReason.CONTENT_FILTER:
                raise RuntimeError("Content filtered by provider")

            if stop_reason == StopReason.MAX_TOKENS and not tool_calls:
                await session.emit("warning", message="Task response truncated (max_tokens)")
                await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                return "".join([*full_text_total, full_text])

            if stop_reason == StopReason.PAUSE_TURN:
                pause_count += 1
                if pause_count > max_pause_continuations:
                    await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                    return "".join([*full_text_total, full_text])
                if not tool_calls:
                    full_text_total.append(full_text)
                    continue

            if not tool_calls:
                await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
                return "".join([*full_text_total, full_text])

            # ─── Process Tool Calls ───────────────────────────────────
            full_text_total.append(full_text)
            pause_count = 0
            progress = self._compute_task_progress(task_id)

            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                tool_id = tc.get("id")
                if not tool_name or not tool_id:
                    continue
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError) as _e:
                    # Preserve a valid provider history when the next tool-loop
                    # iteration serializes this assistant message.
                    tc["function"]["arguments"] = "{}"
                    bad_result = {"error": f"Malformed tool arguments (invalid JSON): {_e}"}
                    await session.emit("tool_call_start", tool=tool_name, input={}, call_id=tool_id, task_id=task_id)
                    await session.emit("tool_call_end", tool=tool_name, output=bad_result, call_id=tool_id, task_id=task_id)
                    messages.append(Message(role="tool", content=_dumps(bad_result), tool_call_id=tool_id, name=tool_name))
                    continue

                await session.emit(
                    "tool_call_start",
                    tool=tool_name,
                    input=args,
                    call_id=tool_id,
                    task_id=task_id,
                    **progress,
                )

                tool = self._tools.get(tool_name)
                if tool is None:
                    result = {"error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        result = await tool.execute(args, task_tool_ctx)
                    except asyncio.CancelledError:
                        result = {"error": "Tool execution interrupted"}
                        await session.emit("tool_call_end", tool=tool_name, output=result, call_id=tool_id, task_id=task_id, status="error")
                        raise
                    except Exception as e:
                        _log.warning("Tool %s failed (call_id=%s): %s", tool_name, tool_id, e, exc_info=True)
                        result = {"error": str(e)[:500]}

                await session.emit(
                    "tool_call_end",
                    tool=tool_name,
                    output=result,
                    call_id=tool_id,
                    task_id=task_id,
                    **progress,
                )

                if result.get("images"):
                    await session.emit("image_output", message_id=message_id, images=result["images"])

                if result.get("files"):
                    _deliverable = [f for f in result["files"] if isinstance(f, dict) and f.get("url")]
                    if _deliverable:
                        await session.emit("file_output", message_id=message_id, files=_deliverable)

                if tool_name == "knowledge_rw" and self._project_context:
                    op = args.get("operation")
                    if op == "write":
                        from .knowledge.loader import update_context_file, _is_log_role
                        slug = args.get("slug", "")
                        content = args.get("content", "")
                        if slug and content and not _is_log_role(content):
                            update_context_file(self._project_context, slug, content)
                            self._invalidate_static_cache()
                            self._mark_prompt_dirty()
                            await session.emit("knowledge_updated")
                    elif op in ("load", "unload", "refresh", "delete", "purge"):
                        self._mark_prompt_dirty()

                if tool_name == "memory_rw":
                    op = args.get("operation")
                    if op in ("save", "update", "archive"):
                        self._invalidate_static_cache()
                        self._mark_prompt_dirty()

                messages.append(Message(
                    role="tool",
                    content=_dumps(result),
                    tool_call_id=tool_id,
                    name=tool_name,
                ))

        # Loop exhausted (max_iterations)
        await session.emit("stream_end", message_id=message_id, total_tokens=session.tokens_used, task_id=task_id)
        return "".join(full_text_total)
