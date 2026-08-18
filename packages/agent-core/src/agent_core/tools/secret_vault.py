"""User key vault tool - list / save / delete user-scoped secrets.

Secrets are stored in the ``user_tokens`` table (AES-256-GCM, per-user).
The agent never sees plaintext: ``save`` collects the value via an
interactive secret prompt that is encrypted server-side before the
Future resolves.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)


class SecretVaultTool(Tool):

    prompt_hint = (
        "Manage the user's personal key vault (list/save/delete); it never returns "
        "plaintext. List it before api_request to find usable secret_ref keys."
    )

    @property
    def name(self) -> str:
        return "secret_vault"

    @property
    def description(self) -> str:
        return (
            "Manage the user's personal key vault (cross-project, per-user encrypted).\n"
            "Operations:\n"
            "- list: show all stored keys (service_key, display_name, key_hint, base_url). Never returns plaintext.\n"
            "- save: prompt the user to enter a secret interactively. Plaintext is encrypted server-side, never seen by the agent.\n"
            "- delete: prompt the user to confirm deletion of a key.\n\n"
            "Use 'list' to check what keys the user already has before calling api_request. "
            "Saved keys can be referenced by api_request via secret_scope='user' and secret_ref=<service_key>."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "save", "delete"],
                    "description": "Operation to perform.",
                },
                "service_key": {
                    "type": "string",
                    "description": "Key identifier (for save/delete). e.g. 'myapi:token', 'jira:email'.",
                },
                "display_name": {
                    "type": "string",
                    "description": "Human-readable label (for save). Optional.",
                },
                "base_url": {
                    "type": "string",
                    "description": "Custom base URL override for the service (for save). Optional.",
                },
            },
            "required": ["operation"],
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        op = params["operation"]
        if op == "list":
            return await self._op_list(params, context)
        elif op == "save":
            return await self._op_save(params, context)
        elif op == "delete":
            return await self._op_delete(params, context)
        return {"error": f"Unknown operation: {op}"}

    async def _op_list(self, params: dict, context: ToolContext) -> dict:
        if not context.db_session:
            return {"error": "No database session available"}

        rows = await context.db_session.execute(
            text(
                "SELECT service_key, display_name, key_hint, base_url "
                "FROM user_tokens WHERE user_id = :uid "
                "ORDER BY service_key"
            ),
            {"uid": context.user_id},
        )
        entries = []
        for row in rows.fetchall():
            entries.append({
                "service_key": row.service_key,
                "display_name": row.display_name,
                "key_hint": row.key_hint,
                "base_url": row.base_url,
            })
        return {"entries": entries, "count": len(entries)}

    async def _op_save(self, params: dict, context: ToolContext) -> dict:
        if context.session is None:
            return {"error": "secret_vault save requires an active conversation session."}

        service_key = params.get("service_key")
        if not service_key:
            return {"error": "service_key is required for save"}

        prompt_id = str(uuid.uuid4())
        try:
            result = await context.session.request_user_selection(
                prompt_id=prompt_id,
                field_key=service_key,
                question=f"请输入密钥 ({service_key})",
                kind="text",
                secret=True,
                service_key=service_key,
                save_to_user_tokens=True,
                task_id=context.current_task_id,
                timeout=300.0,
            )
        except RuntimeError as exc:
            return {"error": str(exc)}

        if result == "secret_saved":
            # Update display_name / base_url if provided
            display_name = params.get("display_name")
            base_url = params.get("base_url")
            if (display_name or base_url) and context.db_session:
                updates: list[str] = []
                sql_params: dict[str, Any] = {"uid": context.user_id, "skey": service_key}
                if display_name:
                    updates.append("display_name = :display_name")
                    sql_params["display_name"] = display_name
                if base_url:
                    updates.append("base_url = :base_url")
                    sql_params["base_url"] = base_url
                if updates:
                    from datetime import datetime, timezone
                    updates.append("updated_at = :now")
                    sql_params["now"] = datetime.now(timezone.utc).isoformat()
                    await context.db_session.execute(
                        text(
                            f"UPDATE user_tokens SET {', '.join(updates)} "
                            "WHERE user_id = :uid AND service_key = :skey"
                        ),
                        sql_params,
                    )
                    await context.db_session.commit()

            return {"status": "saved", "service_key": service_key}
        return {"error": f"Secret save failed: {result}", "service_key": service_key}

    async def _op_delete(self, params: dict, context: ToolContext) -> dict:
        if context.session is None:
            return {"error": "secret_vault delete requires an active conversation session."}

        service_key = params.get("service_key")
        if not service_key:
            return {"error": "service_key is required for delete"}

        prompt_id = str(uuid.uuid4())
        try:
            result = await context.session.request_user_selection(
                prompt_id=prompt_id,
                field_key=f"delete_{service_key}",
                question=f"确认删除密钥 {service_key}？",
                kind="selection",
                options=[
                    {"label": "确认删除", "value": "confirm"},
                    {"label": "取消", "value": "cancel"},
                ],
                allow_other=False,
                task_id=context.current_task_id,
                timeout=120.0,
            )
        except RuntimeError as exc:
            return {"error": str(exc)}

        if result != "confirm":
            return {"status": "cancelled", "service_key": service_key}

        if not context.db_session:
            return {"error": "No database session available"}

        await context.db_session.execute(
            text(
                "DELETE FROM user_tokens WHERE user_id = :uid AND service_key = :skey"
            ),
            {"uid": context.user_id, "skey": service_key},
        )
        await context.db_session.commit()
        return {"status": "deleted", "service_key": service_key}
