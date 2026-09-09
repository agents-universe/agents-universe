"""SchedulerTool — author and manage cron-scheduled project tasks by conversation.

Backs the 定时任务 tab: one task runs an automation script, a generated
Playwright spec, or an agent prompt on a cron schedule and (optionally) posts
the result into a conversation. The tool reaches its DB models through a lazy
``from api.models.schedule import ...`` (the script_writer pattern) so
agent-core keeps no import-time dependency on the API package.

Cron evaluation lives in ``agent_core.scheduling.cron`` and is shared with the
API-side scheduler loop, so what the agent reports as "next run" is exactly
what the loop will fire.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from ..scheduling.cron import CronError, describe_cron, next_run_at, validate_cron
from .base import Tool, ToolContext

_NAME_MAX = 255
_DESCRIPTION_MAX = 2000
_ENV_MAX = 2000
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DEFAULT_TZ = "Asia/Shanghai"

# Credential-shaped strings that must never end up in a scheduled prompt: the
# prompt is stored in plaintext and replayed into the conversation every fire.
_SECRET_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "api-key",
    "access_key", "private_key", "client_secret", "bearer ", "sk-",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret_hint(prompt: str) -> str | None:
    lowered = prompt.lower()
    for hint in _SECRET_HINTS:
        if hint in lowered:
            return hint
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SchedulerTool(Tool):
    """Cron-scheduled project tasks (script / Playwright spec / agent prompt)."""

    name = "scheduler"
    prompt_hint = (
        "Create and manage 定时任务 (cron-scheduled tasks) that run a project "
        "script, a Playwright spec, or an agent prompt on a schedule and post "
        "the result into a conversation. Never put credentials in a scheduled "
        "prompt — it is stored in plaintext."
    )
    description = (
        "Manage cron-scheduled tasks for this project (the 定时任务 tab). "
        "Operations: create (name + target_type + cron_expr [+ timezone, "
        "conversation_id]), list, get, update, delete, enable, disable, run_now "
        "(trigger one immediate run without changing the schedule). Targets: "
        "script (script_id from script_writer), playwright (spec_slug from the "
        "QA-generated specs), agent (agent_slug + prompt). Results are delivered "
        "into conversation_id when set. Missed runs during downtime are skipped, "
        "never replayed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "create", "list", "get", "update", "delete",
                    "enable", "disable", "run_now",
                ],
                "description": "The scheduled-task operation to perform",
            },
            "schedule_id": {
                "type": "string",
                "description": "Task id from create/list (required for get/update/delete/enable/disable/run_now)",
            },
            "name": {
                "type": "string",
                "maxLength": _NAME_MAX,
                "description": "Human-readable task name (create; update optional)",
            },
            "description": {
                "type": "string",
                "maxLength": _DESCRIPTION_MAX,
                "description": "One-line purpose of the schedule",
            },
            "target_type": {
                "type": "string",
                "enum": ["script", "playwright", "agent"],
                "description": "What each fire runs",
            },
            "script_id": {
                "type": "string",
                "description": "Target script (target_type=script); create it with script_writer first",
            },
            "spec_slug": {
                "type": "string",
                "description": "Target Playwright spec slug (target_type=playwright), file tests/generated/{slug}.spec.ts",
            },
            "agent_slug": {
                "type": "string",
                "description": "Target agent slug (target_type=agent), e.g. tech-lead",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Prompt handed to the agent on each fire (target_type=agent). "
                    "Stored in plaintext — never include passwords, tokens or keys."
                ),
            },
            "conversation_id": {
                "type": "string",
                "description": (
                    "Conversation the result is posted into. For agent tasks it is "
                    "also the execution thread; omit it and one is created."
                ),
            },
            "cron_expr": {
                "type": "string",
                "maxLength": 100,
                "description": "5-field cron: minute hour day-of-month month day-of-week (e.g. '0 9 * * 1-5')",
            },
            "timezone": {
                "type": "string",
                "maxLength": 64,
                "description": f"IANA timezone (default {_DEFAULT_TZ})",
            },
            "env": {
                "type": "object",
                "description": "APP_* environment variables for a Playwright target (non-secret)",
            },
            "enabled": {
                "type": "boolean",
                "description": "Whether the schedule is active (create; update optional)",
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if context.db_session is None:
            return {"error": "No database session available"}
        try:
            from api.models.schedule import ScheduledTask, ScheduledTaskRun
        except ImportError:
            return {"error": "Scheduled-task database models unavailable in this environment"}

        operation = params.get("operation")
        try:
            if operation == "create":
                return await self._op_create(params, context, ScheduledTask)
            if operation == "list":
                return await self._op_list(context, ScheduledTask)
            if operation == "get":
                return await self._op_get(params, context, ScheduledTask)
            if operation == "update":
                return await self._op_update(params, context, ScheduledTask)
            if operation == "delete":
                return await self._op_delete(params, context, ScheduledTask)
            if operation in ("enable", "disable"):
                return await self._op_toggle(params, context, ScheduledTask, operation == "enable")
            if operation == "run_now":
                return await self._op_run_now(params, context, ScheduledTask, ScheduledTaskRun)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            try:
                await context.db_session.rollback()
            except Exception:
                pass
            return {"error": str(exc)}
        return {"error": f"Unknown operation {operation!r}"}

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _find(self, context: ToolContext, schedule_id: str, ScheduledTask) -> Any | None:
        result = await context.db_session.execute(
            select(ScheduledTask).where(
                ScheduledTask.schedule_id == schedule_id,
                ScheduledTask.project_id == context.project_id,
            )
        )
        return result.scalar_one_or_none()

    def _check_cron(self, cron_expr: str, timezone_name: str) -> dict[str, Any] | None:
        problem = validate_cron(cron_expr)
        if problem:
            return {"error": f"Invalid cron expression: {problem}"}
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return {"error": f"Unknown timezone: {timezone_name!r}"}
        return None

    async def _check_target(
        self,
        context: ToolContext,
        target_type: str,
        *,
        script_id: str | None,
        spec_slug: str | None,
        agent_slug: str | None,
        prompt: str | None,
    ) -> dict[str, Any] | None:
        """Validate the target exists in THIS project; returns an error dict or None."""
        if target_type == "script":
            if not script_id:
                return {"error": "script_id is required for a script task"}
            from api.models.script import AutomationScript
            row = (
                await context.db_session.execute(
                    select(AutomationScript).where(
                        AutomationScript.script_id == script_id,
                        AutomationScript.project_id == context.project_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return {"error": "Script not found in this project"}
            if row.script_type == "playwright":
                return {"error": "Use target_type=playwright for a Playwright spec"}
        elif target_type == "playwright":
            if not spec_slug or not _SLUG_RE.match(spec_slug):
                return {"error": "spec_slug must be a lowercase slug (a-z, 0-9, -)"}
            if context.project_fs_path:
                from pathlib import Path
                spec = Path(context.project_fs_path) / "tests" / "generated" / f"{spec_slug}.spec.ts"
                if not spec.is_file():
                    return {"error": f"Spec not found: tests/generated/{spec_slug}.spec.ts"}
        elif target_type == "agent":
            if not agent_slug or not _SLUG_RE.match(agent_slug):
                return {"error": "agent_slug must be a lowercase slug (a-z, 0-9, -)"}
            if not (prompt or "").strip():
                return {"error": "prompt is required for an agent task"}
            hint = _secret_hint(prompt or "")
            if hint:
                return {
                    "error": (
                        f"The prompt looks like it contains a credential ({hint!r}). "
                        "Scheduled prompts are stored in plaintext and replayed into "
                        "the conversation — reference project_secrets / user_tokens "
                        "instead and let the run-time tool resolve them."
                    )
                }
            from api.services.agent_sync import resolve_agent_definition_path
            try:
                resolved = resolve_agent_definition_path(agent_slug, context.project_fs_path)
            except ValueError:
                return {"error": "agent_slug is not a safe slug"}
            if resolved is None:
                return {"error": f"No agent definition found for {agent_slug!r}"}
        else:
            return {"error": f"target_type must be script, playwright or agent, got {target_type!r}"}
        return None

    async def _check_conversation(
        self, context: ToolContext, conversation_id: str | None
    ) -> dict[str, Any] | None:
        if not conversation_id:
            return None
        from api.models.conversation import Conversation
        row = (
            await context.db_session.execute(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.project_id == context.project_id,
                    Conversation.user_id == context.user_id,
                    Conversation.status == "active",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"error": "Target conversation not found in this project for the current user"}
        return None

    def _serialize(self, task: Any) -> dict[str, Any]:
        return {
            "schedule_id": str(task.schedule_id),
            "name": task.name,
            "description": task.description,
            "target_type": task.target_type,
            "script_id": str(task.script_id) if task.script_id else None,
            "spec_slug": task.spec_slug,
            "agent_slug": task.agent_slug,
            "conversation_id": task.conversation_id,
            "cron_expr": task.cron_expr,
            "timezone": task.timezone,
            "enabled": bool(task.enabled),
            "next_run_at": _iso(task.next_run_at),
            "schedule": describe_cron(task.cron_expr),
            "last_run_at": _iso(task.last_run_at),
            "last_status": task.last_status,
        }

    # ── operations ───────────────────────────────────────────────────────────

    async def _op_create(self, params: dict[str, Any], context: ToolContext, ScheduledTask) -> dict[str, Any]:
        name = (params.get("name") or "").strip()
        target_type = params.get("target_type") or ""
        cron_expr = (params.get("cron_expr") or "").strip()
        timezone_name = params.get("timezone") or _DEFAULT_TZ
        description = params.get("description")
        conversation_id = params.get("conversation_id")
        enabled = params.get("enabled", True)

        if not name:
            return {"error": "name is required"}
        if len(name) > _NAME_MAX:
            return {"error": f"name must be at most {_NAME_MAX} characters"}
        if description and len(description) > _DESCRIPTION_MAX:
            return {"error": f"description must be at most {_DESCRIPTION_MAX} characters"}
        if not cron_expr:
            return {"error": "cron_expr is required (5 fields: minute hour day month weekday)"}
        problem = self._check_cron(cron_expr, timezone_name)
        if problem:
            return problem
        target_error = await self._check_target(
            context, target_type,
            script_id=params.get("script_id"),
            spec_slug=params.get("spec_slug"),
            agent_slug=params.get("agent_slug"),
            prompt=params.get("prompt"),
        )
        if target_error:
            return target_error
        conv_error = await self._check_conversation(context, conversation_id)
        if conv_error:
            return conv_error

        env = params.get("env") or {}
        if env and not isinstance(env, dict):
            return {"error": "env must be an object of APP_* string values"}
        env_json = json.dumps(env) if env else None
        if env_json and len(env_json) > _ENV_MAX:
            return {"error": f"env is too large (max {_ENV_MAX} characters as JSON)"}

        row = ScheduledTask(
            project_id=context.project_id,
            name=name,
            description=description or None,
            target_type=target_type,
            script_id=params.get("script_id") if target_type == "script" else None,
            spec_slug=params.get("spec_slug") if target_type == "playwright" else None,
            agent_slug=params.get("agent_slug") if target_type == "agent" else None,
            prompt=params.get("prompt") if target_type == "agent" else None,
            env_json=env_json,
            conversation_id=conversation_id or None,
            cron_expr=cron_expr,
            timezone=timezone_name,
            enabled=bool(enabled),
            next_run_at=next_run_at(cron_expr, timezone_name, _now()) if enabled else None,
            created_by=context.user_id,
        )
        context.db_session.add(row)
        await context.db_session.commit()
        return {"success": True, **self._serialize(row)}

    async def _op_list(self, context: ToolContext, ScheduledTask) -> dict[str, Any]:
        rows = (
            await context.db_session.execute(
                select(ScheduledTask)
                .where(ScheduledTask.project_id == context.project_id)
                .order_by(ScheduledTask.created_at.desc())
            )
        ).scalars().all()
        return {"tasks": [self._serialize(t) for t in rows], "count": len(rows)}

    async def _op_get(self, params: dict[str, Any], context: ToolContext, ScheduledTask) -> dict[str, Any]:
        schedule_id = params.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required"}
        row = await self._find(context, schedule_id, ScheduledTask)
        if row is None:
            return {"error": "Scheduled task not found in this project"}
        return self._serialize(row)

    async def _op_update(self, params: dict[str, Any], context: ToolContext, ScheduledTask) -> dict[str, Any]:
        schedule_id = params.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required"}
        row = await self._find(context, schedule_id, ScheduledTask)
        if row is None:
            return {"error": "Scheduled task not found in this project"}

        target_type = params.get("target_type") or row.target_type
        script_id = params.get("script_id") if params.get("script_id") is not None else row.script_id
        spec_slug = params.get("spec_slug") if params.get("spec_slug") is not None else row.spec_slug
        agent_slug = params.get("agent_slug") if params.get("agent_slug") is not None else row.agent_slug
        prompt = params.get("prompt") if params.get("prompt") is not None else row.prompt
        cron_expr = params.get("cron_expr") or row.cron_expr
        timezone_name = params.get("timezone") or row.timezone
        enabled = row.enabled if params.get("enabled") is None else bool(params["enabled"])
        conversation_id = (
            params.get("conversation_id")
            if params.get("conversation_id") is not None
            else row.conversation_id
        )

        problem = self._check_cron(cron_expr, timezone_name)
        if problem:
            return problem
        target_error = await self._check_target(
            context, target_type,
            script_id=str(script_id) if script_id else None,
            spec_slug=spec_slug,
            agent_slug=agent_slug,
            prompt=prompt,
        )
        if target_error:
            return target_error
        conv_error = await self._check_conversation(context, conversation_id)
        if conv_error:
            return conv_error

        if params.get("name") is not None:
            name = str(params["name"]).strip()
            if not name or len(name) > _NAME_MAX:
                return {"error": f"name must be 1-{_NAME_MAX} characters"}
            row.name = name
        if params.get("description") is not None:
            description = params["description"] or None
            if description and len(description) > _DESCRIPTION_MAX:
                return {"error": f"description must be at most {_DESCRIPTION_MAX} characters"}
            row.description = description
        if params.get("env") is not None:
            env = params["env"] or {}
            if not isinstance(env, dict):
                return {"error": "env must be an object of APP_* string values"}
            row.env_json = json.dumps(env) if env else None

        row.target_type = target_type
        row.script_id = script_id if target_type == "script" else None
        row.spec_slug = spec_slug if target_type == "playwright" else None
        row.agent_slug = agent_slug if target_type == "agent" else None
        row.prompt = prompt if target_type == "agent" else None
        row.conversation_id = conversation_id or None
        row.cron_expr = cron_expr
        row.timezone = timezone_name
        row.enabled = enabled
        row.next_run_at = next_run_at(cron_expr, timezone_name, _now()) if enabled else None
        row.updated_at = _now()
        await context.db_session.commit()
        return {"success": True, **self._serialize(row)}

    async def _op_delete(self, params: dict[str, Any], context: ToolContext, ScheduledTask) -> dict[str, Any]:
        schedule_id = params.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required"}
        row = await self._find(context, schedule_id, ScheduledTask)
        if row is None:
            return {"error": "Scheduled task not found in this project"}
        name = row.name
        await context.db_session.delete(row)
        await context.db_session.commit()
        return {"success": True, "schedule_id": schedule_id, "name": name}

    async def _op_toggle(
        self, params: dict[str, Any], context: ToolContext, ScheduledTask, enabled: bool
    ) -> dict[str, Any]:
        schedule_id = params.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required"}
        row = await self._find(context, schedule_id, ScheduledTask)
        if row is None:
            return {"error": "Scheduled task not found in this project"}
        row.enabled = enabled
        try:
            row.next_run_at = (
                next_run_at(row.cron_expr, row.timezone, _now()) if enabled else None
            )
        except CronError as exc:
            return {"error": f"Stored cron expression is invalid: {exc}"}
        row.updated_at = _now()
        await context.db_session.commit()
        return {"success": True, **self._serialize(row)}

    async def _op_run_now(
        self, params: dict[str, Any], context: ToolContext, ScheduledTask, ScheduledTaskRun
    ) -> dict[str, Any]:
        schedule_id = params.get("schedule_id")
        if not schedule_id:
            return {"error": "schedule_id is required"}
        row = await self._find(context, schedule_id, ScheduledTask)
        if row is None:
            return {"error": "Scheduled task not found in this project"}
        # Flush the pending update state before the launch path opens its own
        # session (it re-reads the row and would otherwise miss it).
        await context.db_session.commit()

        try:
            from api.services.scheduled_runs import ScheduleBusy, spawn_run
        except ImportError:
            return {"error": "Scheduled-run service unavailable in this environment"}
        try:
            run_id = await spawn_run(context.app, schedule_id, trigger="manual")
        except ScheduleBusy:
            return {"error": "This task already has a run in progress"}
        return {
            "success": True,
            "schedule_id": schedule_id,
            "run_id": run_id,
            "note": "The run is queued; its result appears in the 定时任务 tab and in the task's conversation.",
        }
