"""user_confirm tool — pause the agent and show the user an interactive selection card."""
from __future__ import annotations

import uuid
from typing import Any

from .base import Tool, ToolContext


class UserConfirmTool(Tool):
    """Present the user with a selection dialog and wait for their choice.

    The agent pauses until the user picks an option or enters custom text.
    Any agent that needs interactive confirmation adds this to its tools list.
    """

    prompt_hint = (
        "Pause to ask the user — choices, free text, or secure secret entry. Use it for "
        "decisions only the user can make (or required confirmations) instead of guessing."
    )

    @property
    def name(self) -> str:
        return "user_confirm"

    @property
    def description(self) -> str:
        return (
            "Pause and show the user an inline prompt. Supports selection cards, free-text input, "
            "and secure secret collection. For secrets, plaintext is never returned — the value "
            "is saved server-side to project secrets or user tokens and only an opaque status is returned."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user, shown above the options.",
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["label", "value"],
                    },
                    "description": "Options to present. Can be empty for a free-text-only prompt.",
                },
                "field_key": {
                    "type": "string",
                    "description": "Optional identifier for what is being confirmed, e.g. 'jira_project_key'.",
                },
                "allow_other": {
                    "type": "boolean",
                    "description": "Whether to show an 'Other / custom input' option. Defaults to true.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["selection", "text"],
                    "description": "Prompt kind: 'selection' (radio options) or 'text' (free-text input). Default: selection.",
                },
                "title": {
                    "type": "string",
                    "description": "Dialog title displayed above the question.",
                },
                "message": {
                    "type": "string",
                    "description": "Additional context message shown in the dialog.",
                },
                "secret": {
                    "type": "boolean",
                    "description": "If true, show a password input. Value is saved server-side, never returned to the agent.",
                },
                "service_key": {
                    "type": "string",
                    "description": "Service key for secret storage (required when secret=true). e.g. 'third_party:crm:uat'.",
                },
                "environment": {
                    "type": "string",
                    "description": "Environment qualifier for the secret (e.g. 'dev', 'uat', 'prd').",
                },
                "save_to_project_secrets": {
                    "type": "boolean",
                    "description": "If true, save the collected secret to project secrets. Mutually exclusive with save_to_user_tokens.",
                },
                "save_to_user_tokens": {
                    "type": "boolean",
                    "description": "If true, save the collected secret to the user's personal key vault (user_tokens). Mutually exclusive with save_to_project_secrets.",
                },
            },
            "required": ["question"],
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if context.session is None:
            return {
                "error": "user_confirm requires an active conversation session. "
                         "Ensure the tool is called from within an agent run.",
            }

        is_secret = params.get("secret", False)
        service_key = params.get("service_key", "")
        save_to_project = params.get("save_to_project_secrets", False)
        save_to_user = params.get("save_to_user_tokens", False)

        if is_secret and not service_key:
            return {"error": "service_key is required when secret=true"}
        if is_secret and not save_to_project and not save_to_user:
            return {"error": "Either save_to_project_secrets or save_to_user_tokens must be true when secret=true"}
        if is_secret and save_to_project and save_to_user:
            return {"error": "save_to_project_secrets and save_to_user_tokens are mutually exclusive"}

        question = params["question"]
        options = params.get("options", [])
        field_key = params.get("field_key", service_key or "")
        allow_other = params.get("allow_other", True)

        prompt_id = str(uuid.uuid4())

        kwargs: dict[str, Any] = {
            "prompt_id": prompt_id,
            "field_key": field_key,
            "question": question,
            "options": options,
            "allow_other": allow_other,
        }

        if params.get("kind"):
            kwargs["kind"] = params["kind"]
        if params.get("title"):
            kwargs["title"] = params["title"]
        if params.get("message"):
            kwargs["message"] = params["message"]
        if is_secret:
            kwargs["secret"] = True
            kwargs["service_key"] = service_key
            if save_to_user:
                kwargs["save_to_user_tokens"] = True
            else:
                kwargs["save_to_project_secrets"] = True
        if params.get("environment"):
            kwargs["environment"] = params["environment"]

        try:
            selected_value = await context.session.request_user_selection(**kwargs)
        except RuntimeError as exc:
            return {"error": str(exc)}

        if is_secret:
            # The frontend cancels with value='__cancelled__' on a *non-secret*
            # response frame, so it never reaches the secret-save path — the
            # old code then reported "secret_saved" even though the user
            # dismissed the dialog and no secret was stored.
            if selected_value == "__cancelled__":
                return {
                    "field_key": field_key,
                    "secret_ref": service_key,
                    "secret_scope": "user" if save_to_user else "project",
                    "cancelled": True,
                    "status": "cancelled",
                }
            return {
                "field_key": field_key,
                "secret_ref": service_key,
                "secret_scope": "user" if save_to_user else "project",
                # Only two statuses exist (session.py resolve_user_selection_secret);
            # anything else is a protocol drift — report failure rather than
            # claiming a secret was stored when we cannot confirm it.
            "status": selected_value if selected_value in ("secret_saved", "secret_save_failed") else "secret_save_failed",
            }

        if selected_value == "__cancelled__":
            return {
                "field_key": field_key,
                "cancelled": True,
                "selected_value": None,
            }

        return {
            "field_key": field_key,
            "selected_value": selected_value,
        }
