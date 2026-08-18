"""Planner tool — generate a structured task list for complex goals."""
from __future__ import annotations

import json
import uuid
from typing import Any

from .base import Tool, ToolContext


class PlannerTool(Tool):
    """Generates a structured task list. The agent-core loop intercepts this tool call
    to switch into Task mode — it doesn't call an external service."""

    name = "plan_task"
    prompt_hint = (
        "Required before multi-step or implementation work: break the goal into small "
        "tasks, each with a concrete verification method. Skip it for simple Q&A or "
        "single-step requests."
    )
    description = (
        "Break a complex goal into an ordered list of small, independently verifiable tasks. "
        "Call this when the user's request involves 3 or more distinct actions, "
        "multiple tools, sequential steps, or any implementation work. Each task must "
        "state a concrete verification method before execution and should produce one "
        "small, reviewable change set."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The high-level goal to decompose into tasks",
            },
            "tasks": {
                "type": "array",
                "description": "The structured task list",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "tools_needed": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "estimated_complexity": {
                            "type": "string",
                            "enum": ["low", "mid", "high"],
                        },
                    },
                    "required": ["id", "title", "tools_needed", "depends_on", "estimated_complexity"],
                },
            },
        },
        "required": ["goal", "tasks"],
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # Assign stable IDs if not provided
        tasks = params.get("tasks", [])
        for task in tasks:
            if not task.get("id"):
                task["id"] = f"t{uuid.uuid4().hex[:6]}"
        return {
            "accepted": True,
            "task_count": len(tasks),
            "tasks": tasks,
        }
