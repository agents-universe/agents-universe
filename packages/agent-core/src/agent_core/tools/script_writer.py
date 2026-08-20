"""ScriptWriterTool — create, run and iterate on persisted project scripts.

Persists python/bash scripts to the automation_scripts table so they appear in
the web UI's "Script Executor" page, and runs them through the same sandboxed
runner the page uses (agent_core.scripts.runner) — the agent's runs and the
human's runs share one concurrency slot and one execution path.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..scripts.runner import script_slot_guard
from .base import Tool, ToolContext

# Column-width mirrors of api/models/script.py (the pydantic ScriptCreate
# caps live in the API router; the tool pre-checks the same limits).
_NAME_MAX = 255
_DESCRIPTION_MAX = 2000

# Tail of the combined run log returned to the model for failure iteration.
_LOG_TAIL = 4000

# Poll cadence for a spawned run. The executor caps a script at 300s plus
# teardown, so poll slightly longer (155 x 2s) before giving up on the tool
# call — the run itself keeps going in the background either way.
_POLL_INTERVAL = 2
_POLL_LOOPS = 155


def _tail(text: str, limit: int = _LOG_TAIL) -> str:
    """Last `limit` chars, with a truncation marker when longer."""
    if len(text) <= limit:
        return text
    return "…[truncated]\n" + text[-limit:]


def _log_task_error(task: asyncio.Task) -> None:
    """Consume a spawned runner task's exception so it is logged, not dropped."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logging.getLogger("agents_universe.scripts").error("Script execution failed: %s", exc, exc_info=exc)


class ScriptWriterTool(Tool):
    """Persisted project automation scripts (python/bash), runnable from the
    Script Executor page."""

    name = "script_writer"
    prompt_hint = (
        "Create/update/run reusable project scripts (python/bash) saved to the "
        "Script Executor page. Prototype fast with code_executor first, then "
        "persist and verify through script_writer (real 300s sandbox)."
    )
    description = (
        "Manage persisted project automation scripts (python or bash) that appear "
        "in the Script Executor page. Operations: create (save a new script), "
        "list (scripts in this project), get (full content of one script), update "
        "(edit name/description/type/content), run (execute through the sandboxed "
        "runner and return the output tail for iteration)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create", "list", "get", "update", "run"],
                "description": "The script operation to perform",
            },
            "script_id": {
                "type": "string",
                "description": "Script id returned by create/list (required for get/update/run)",
            },
            "name": {
                "type": "string",
                "maxLength": _NAME_MAX,
                "description": "Script name shown in the Script Executor list (create; update optional)",
            },
            "script_type": {
                "type": "string",
                "enum": ["python", "bash"],
                "default": "python",
                "description": (
                    "python scripts are confined to the project workspace and "
                    "cannot spawn subprocesses; bash blocks external commands "
                    "(curl/wget/node/python3/ssh/...)"
                ),
            },
            "description": {
                "type": "string",
                "maxLength": _DESCRIPTION_MAX,
                "description": "One-line purpose shown in the Script Executor list — always describe what the script does",
            },
            "content": {
                "type": "string",
                "description": "Full script source code (create/update)",
            },
        },
        "required": ["operation"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if context.db_session is None:
            return {"error": "No database session available"}
        try:
            from api.models.script import AutomationScript, ScriptRun
        except ImportError:
            return {"error": "Script database models unavailable in this environment"}

        operation = params.get("operation")
        try:
            if operation == "create":
                return await self._op_create(params, context, AutomationScript)
            if operation == "list":
                return await self._op_list(context, AutomationScript)
            if operation == "get":
                return await self._op_get(params, context, AutomationScript)
            if operation == "update":
                return await self._op_update(params, context, AutomationScript)
            if operation == "run":
                return await self._op_run(params, context, AutomationScript, ScriptRun)
        except Exception as exc:
            # Rollback so a failed op never leaves the shared session in a
            # broken state for the rest of the turn. CancelledError is not
            # caught here — it must propagate (the run task's done_callback
            # releases the slot, so cancellation stays leak-free).
            try:
                await context.db_session.rollback()
            except Exception:
                pass
            return {"error": str(exc)}
        return {"error": f"Unknown operation {operation!r}"}

    async def _find_script(self, context: ToolContext, script_id: str, AutomationScript) -> Any | None:
        result = await context.db_session.execute(
            select(AutomationScript).where(
                AutomationScript.script_id == script_id,
                AutomationScript.project_id == context.project_id,
            )
        )
        return result.scalar_one_or_none()

    async def _op_create(self, params: dict[str, Any], context: ToolContext, AutomationScript) -> dict[str, Any]:
        name = (params.get("name") or "").strip()
        content = params.get("content") or ""
        script_type = params.get("script_type") or "python"
        description = params.get("description")

        if not name:
            return {"error": "name is required"}
        if len(name) > _NAME_MAX:
            return {"error": f"name must be at most {_NAME_MAX} characters"}
        if not content:
            return {"error": "content is required"}
        if script_type not in ("python", "bash"):
            return {"error": f"script_type must be 'python' or 'bash', got {script_type!r}"}
        if description and len(description) > _DESCRIPTION_MAX:
            return {"error": f"description must be at most {_DESCRIPTION_MAX} characters"}

        row = AutomationScript(
            project_id=context.project_id,
            name=name,
            description=description or None,
            script_type=script_type,
            content=content,
            created_by=context.user_id,
        )
        context.db_session.add(row)
        await context.db_session.commit()
        return {
            "success": True,
            "script_id": str(row.script_id),
            "name": row.name,
            "script_type": row.script_type,
        }

    async def _op_list(self, context: ToolContext, AutomationScript) -> dict[str, Any]:
        result = await context.db_session.execute(
            select(AutomationScript)
            .where(
                AutomationScript.project_id == context.project_id,
                AutomationScript.script_type != "playwright",
            )
            .order_by(AutomationScript.created_at.desc())
        )
        scripts = result.scalars().all()
        return {
            "scripts": [
                {
                    "script_id": str(s.script_id),
                    "name": s.name,
                    "script_type": s.script_type,
                    "description": s.description,
                }
                for s in scripts
            ],
            "count": len(scripts),
        }

    async def _op_get(self, params: dict[str, Any], context: ToolContext, AutomationScript) -> dict[str, Any]:
        script_id = params.get("script_id")
        if not script_id:
            return {"error": "script_id is required"}
        row = await self._find_script(context, script_id, AutomationScript)
        if row is None:
            return {"error": "Script not found in this project"}
        return {
            "script_id": str(row.script_id),
            "name": row.name,
            "script_type": row.script_type,
            "description": row.description,
            "content": row.content,
        }

    async def _op_update(self, params: dict[str, Any], context: ToolContext, AutomationScript) -> dict[str, Any]:
        script_id = params.get("script_id")
        if not script_id:
            return {"error": "script_id is required"}
        fields = {key: params[key] for key in ("name", "description", "script_type", "content") if params.get(key) is not None}
        if not fields:
            return {"error": "Nothing to update — provide name, description, script_type or content"}

        row = await self._find_script(context, script_id, AutomationScript)
        if row is None:
            return {"error": "Script not found in this project"}

        if "name" in fields:
            name = str(fields["name"]).strip()
            if not name:
                return {"error": "name cannot be empty"}
            if len(name) > _NAME_MAX:
                return {"error": f"name must be at most {_NAME_MAX} characters"}
            row.name = name
        if "description" in fields:
            description = fields["description"] or None
            if description and len(description) > _DESCRIPTION_MAX:
                return {"error": f"description must be at most {_DESCRIPTION_MAX} characters"}
            row.description = description
        if "script_type" in fields:
            if fields["script_type"] not in ("python", "bash"):
                return {"error": f"script_type must be 'python' or 'bash', got {fields['script_type']!r}"}
            row.script_type = fields["script_type"]
        if "content" in fields:
            row.content = fields["content"]

        row.updated_at = datetime.now(timezone.utc)
        await context.db_session.commit()
        return {
            "success": True,
            "script_id": str(row.script_id),
            "name": row.name,
            "script_type": row.script_type,
        }

    async def _op_run(self, params: dict[str, Any], context: ToolContext, AutomationScript, ScriptRun) -> dict[str, Any]:
        script_id = params.get("script_id")
        if not script_id:
            return {"error": "script_id is required"}
        row = await self._find_script(context, script_id, AutomationScript)
        if row is None:
            return {"error": "Script not found in this project"}
        if row.script_type == "playwright":
            return {"error": "Playwright specs are run via the specs endpoint"}
        if not context.project_fs_path:
            return {"error": "Script execution requires a project workspace"}

        from ..scripts.runner import execute_script

        # Same concurrency gate as human runs: agent runs and page runs share
        # one slot pool, so neither can exhaust the server's memory.
        sem = script_slot_guard()
        await sem.acquire()
        run_id: str | None = None
        try:
            run_row = ScriptRun(
                script_id=script_id,
                triggered_by=context.user_id,
                status="pending",
                started_at=datetime.now(timezone.utc),
            )
            context.db_session.add(run_row)
            await context.db_session.commit()
            # Capture the id BEFORE any error path can roll back: a rollback
            # expires the ORM instance and reading run_id afterwards raises
            # MissingGreenlet under the async driver.
            run_id = str(run_row.run_id)
        except BaseException as exc:
            try:
                await context.db_session.rollback()
            except Exception:
                pass
            sem.release()
            return {"error": f"Could not start the run: {exc}"}

        task = asyncio.create_task(
            execute_script(
                run_id, row.content, row.script_type,
                context.user_id, context.project_fs_path, context.db_session_factory,
            )
        )
        task.add_done_callback(_log_task_error)
        # Release the slot when the run finishes (success, failure, or cancel).
        task.add_done_callback(lambda t: sem.release())

        # Poll for a terminal state with fresh sessions — a re-select on the
        # shared session returns the stale identity-mapped "pending" instance
        # (select() does not refresh by default; the runner commits from its
        # own session).
        for _ in range(_POLL_LOOPS):
            run = await self._fetch_run(context, ScriptRun, run_id)
            if run is not None:
                status = run["status"]
                if status in ("completed", "failed"):
                    return {
                        "success": True,
                        "run_id": run_id,
                        "status": status,
                        "exit_code": run["exit_code"],
                        "stdout_tail": _tail(run["stdout_log"]),
                        "stderr_tail": _tail(run["stderr_log"]),
                        "log_tail": _tail(run["stdout_log"] + run["stderr_log"]),
                    }
            await asyncio.sleep(_POLL_INTERVAL)

        # The executor's own 300s cap plus teardown exceeded the poll window —
        # the run keeps going in the background and stays visible in the page.
        return {
            "run_id": run_id,
            "status": "running",
            "note": "Run continues in the background — open the Script Executor page to watch it live, then call script_writer run again to fetch the result.",
        }

    async def _fetch_run(self, context: ToolContext, ScriptRun, run_id: str) -> dict[str, Any] | None:
        if context.db_session_factory is not None:
            async with context.db_session_factory() as session:
                result = await session.execute(select(ScriptRun).where(ScriptRun.run_id == run_id))
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                return {
                    "status": row.status,
                    "exit_code": row.exit_code,
                    "stdout_log": row.stdout_log or "",
                    "stderr_log": row.stderr_log or "",
                }
        # No factory (standalone use): force a refresh of the shared session's
        # identity map so the poll sees the runner's committed writes.
        result = await context.db_session.execute(
            select(ScriptRun)
            .where(ScriptRun.run_id == run_id)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": row.status,
            "exit_code": row.exit_code,
            "stdout_log": row.stdout_log or "",
            "stderr_log": row.stderr_log or "",
        }
