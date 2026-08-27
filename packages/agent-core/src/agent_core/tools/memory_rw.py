"""Agent tool for reading and writing memories across all three layers."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

MAX_SESSION_NOTES = 20
MAX_NOTE_LENGTH = 200

_SECRET_PATTERN = re.compile(
    r"TOKEN|PASSWORD|SECRET|COOKIE|API_KEY|PRIVATE_KEY|CLIENT_SECRET|BEARER|CREDENTIAL|AUTH",
    re.IGNORECASE,
)


def _coerce_tags(value: Any) -> list[str]:
    """Normalize the tags param to a list of strings.

    The schema declares an array, but LLMs routinely pass a bare string
    ("important,qa" or "important"). Storing that raw would serialize a JSON
    string instead of an array — recall then treats it as a substring match
    and returns wrong memories. Same defense as confluence's page_ids.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t) for t in value if t is not None]
    return []


class MemoryRWTool(Tool):

    prompt_hint = (
        "Store durable user/project facts (personal layer) or scratch notes for this "
        "conversation (session layer). Secret-looking values are rejected by design — "
        "store credentials via secret_vault or api_request secret_refs instead."
    )

    @property
    def name(self) -> str:
        return "memory_rw"

    @property
    def description(self) -> str:
        return (
            "Save, recall, update, or archive memories. Three layers:\n"
            "- session: ephemeral notes for this conversation (not persisted)\n"
            "- personal: persistent user/project facts (injected into system prompt)\n"
            "- episodic: past conversation summaries (read-only recall)\n\n"
            "Never store secrets, tokens, or passwords in memories."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["save", "save_session", "recall", "recall_episodes", "update", "archive"],
                    "description": "Operation to perform.",
                },
                "content": {
                    "type": "string",
                    "description": "Memory content (for save/update).",
                },
                "note": {
                    "type": "string",
                    "description": "Short session note (for save_session, max 200 chars).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for organization (for save/update/recall filter).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "global", "session", "personal", "all"],
                    "description": "Scope — for save: 'project'|'global'; for recall: 'session'|'personal'|'all'.",
                },
                "query": {
                    "type": "string",
                    "description": "Search term for recall (matches content via LIKE).",
                },
                "memory_id": {
                    "type": "string",
                    "description": "ID of a specific personal memory (for update/archive).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results for recall/recall_episodes (default 10).",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["default", "project_setting"],
                    "description": "Memory type. 'project_setting' auto-generates structured content and tags.",
                },
                "key": {
                    "type": "string",
                    "description": "Setting key (for memory_type=project_setting). e.g. JIRA_PROJECT_KEY",
                },
                "value": {
                    "type": "string",
                    "description": "Setting value (for memory_type=project_setting).",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain tag (for memory_type=project_setting). e.g. issue-tracker",
                },
            },
            "required": ["operation"],
        }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        op = params["operation"]
        dispatch = {
            "save": self._op_save,
            "save_session": self._op_save_session,
            "recall": self._op_recall,
            "recall_episodes": self._op_recall_episodes,
            "update": self._op_update,
            "archive": self._op_archive,
        }
        handler = dispatch.get(op)
        if not handler:
            return {"error": f"Unknown operation: {op}"}
        try:
            return await handler(params, context)
        except Exception as e:
            _log.warning("memory_rw %s failed: %s", op, e, exc_info=True)
            return {"error": f"Memory operation failed: {e}"}

    def _check_secret_guard(self, *values: str | None) -> str | None:
        for v in values:
            if v and _SECRET_PATTERN.search(v):
                return (
                    "Rejected: value looks like a secret (matches TOKEN/PASSWORD/SECRET/etc). "
                    "Secrets must be stored in project secrets or user token vault, not personal memory."
                )
        return None

    async def _op_save(self, params: dict, context: ToolContext) -> dict:
        memory_type = params.get("memory_type", "default")

        if memory_type == "project_setting":
            key = params.get("key")
            value = params.get("value")
            if not key or not value:
                return {"error": "key and value are required for memory_type=project_setting"}
            scope = "project"
            guard_error = self._check_secret_guard(key)
            if guard_error:
                return {"error": guard_error}
            content = f"PROJECT_SETTING {key}={value}"
            tags = ["project-setting", f"config:{key}"]
            domain = params.get("domain")
            if domain:
                tags.append(domain)
        else:
            content = params.get("content")
            if not content:
                return {"error": "content is required for save"}
            tags = _coerce_tags(params.get("tags"))
            scope = params.get("scope", "project")
            # The parameters schema lists recall scopes too — a scope like
            # "session"/"all" silently stored the memory under the project
            # scope. Reject instead of guessing.
            if scope not in ("project", "global"):
                return {"error": f"save supports scope 'project' or 'global' only, got {scope!r}"}

        guard_error = self._check_secret_guard(content)
        if guard_error:
            return {"error": guard_error}

        from sqlalchemy import text

        project_id = None if scope == "global" else context.project_id

        from agent_core.tools._compat import new_uuid, now_iso

        memory_id = new_uuid()
        now = now_iso()

        await context.db_session.execute(
            text(
                "INSERT INTO personal_memories "
                "(memory_id, user_id, project_id, content, tags, created_by, created_at, is_archived) "
                "VALUES (:mid, :uid, :pid, :content, :tags, :created_by, :created_at, 0)"
            ),
            {
                "mid": memory_id,
                "uid": context.user_id,
                "pid": project_id,
                "content": content,
                "tags": json.dumps(tags) if tags else None,
                "created_by": f"agent",
                "created_at": now,
            },
        )
        await context.db_session.commit()

        if context.session:
            await context.session.emit(
                "memory_saved",
                memory_id=memory_id,
                content=content,
                tags=tags,
                created_by="agent",
            )

        return {"success": True, "memory_id": memory_id, "scope": scope}

    async def _op_save_session(self, params: dict, context: ToolContext) -> dict:
        note = params.get("note") or params.get("content", "")
        if not note:
            return {"error": "note is required for save_session"}

        note = note[:MAX_NOTE_LENGTH]
        session_memories = context.session_memories

        if len(session_memories) >= MAX_SESSION_NOTES:
            session_memories.pop(0)

        entry = {"note": note, "timestamp": time.time()}
        session_memories.append(entry)

        if context.session:
            await context.session.emit(
                "session_memory_added",
                note=note,
                timestamp=entry["timestamp"],
            )

        return {"success": True, "count": len(session_memories)}

    async def _op_recall(self, params: dict, context: ToolContext) -> dict:
        scope = params.get("scope", "all")
        query = params.get("query")
        tags_filter = _coerce_tags(params.get("tags"))
        # Clamp hard: a negative limit would slice from the END of the result
        # list (results[:-3] drops the newest entries), zero would return
        # nothing, and non-numeric input would TypeError.
        try:
            limit = max(1, min(int(params.get("limit") or 10), 50))
        except (TypeError, ValueError):
            limit = 10

        results: list[dict] = []

        if scope in ("session", "all"):
            session_notes = context.session_memories
            for entry in session_notes:
                if query and query.lower() not in entry["note"].lower():
                    continue
                results.append({"layer": "session", "note": entry["note"], "timestamp": entry["timestamp"]})

        if scope in ("personal", "all"):
            from sqlalchemy import text

            sql = (
                "SELECT memory_id, content, tags, created_by, created_at "
                "FROM personal_memories "
                "WHERE user_id = :uid "
                "AND (project_id = :pid OR project_id IS NULL) "
                "AND is_archived = :archived "
            )
            sql_params: dict[str, Any] = {"uid": context.user_id, "pid": context.project_id, "archived": False}

            if query:
                sql += "AND content LIKE :q "
                sql_params["q"] = f"%{query}%"

            sql += "ORDER BY created_at DESC"

            rows = await context.db_session.execute(text(sql), sql_params)
            for row in rows.fetchall():
                row_tags = []
                try:
                    row_tags = json.loads(row.tags) if row.tags else []
                except (json.JSONDecodeError, TypeError):
                    pass
                if tags_filter and not any(t in row_tags for t in tags_filter):
                    continue
                results.append({
                    "layer": "personal",
                    "memory_id": row.memory_id,
                    "content": row.content,
                    "tags": row_tags,
                    "created_by": row.created_by,
                })
                if len(results) >= limit:
                    break

        return {"memories": results[:limit]}

    async def _op_recall_episodes(self, params: dict, context: ToolContext) -> dict:
        try:
            limit = max(1, min(int(params.get("limit") or 5), 20))
        except (TypeError, ValueError):
            limit = 5

        from sqlalchemy import text

        # Fetch newest first with the limit pushed into SQL: slicing in
        # Python after fetchall() pulled every episode row into memory.
        # MSSQL has no LIMIT and SQLite has no OFFSET/FETCH — pick per dialect
        # (the previous OFFSET/FETCH-only form failed on SQLite dev/test DBs).
        bind = context.db_session.bind
        if bind is not None and bind.dialect.name == "mssql":
            limit_clause = "OFFSET 0 ROWS FETCH NEXT :lim ROWS ONLY"
        else:
            limit_clause = "LIMIT :lim"
        rows = await context.db_session.execute(
            text(
                "SELECT episode_id, conversation_id, summary, key_findings, open_questions, created_at "
                "FROM episodic_memories "
                "WHERE user_id = :uid AND (project_id = :pid OR (:pid IS NULL AND project_id IS NULL)) "
                f"ORDER BY created_at DESC {limit_clause}"
            ),
            {"uid": context.user_id, "pid": context.project_id, "lim": limit},
        )
        episodes = []
        for row in rows.fetchall():
            try:
                kf = json.loads(row.key_findings) if row.key_findings else []
            except (json.JSONDecodeError, TypeError):
                kf = []
            try:
                oq = json.loads(row.open_questions) if row.open_questions else []
            except (json.JSONDecodeError, TypeError):
                oq = []
            # Normalize to UTC ISO: raw SQL bypasses the ORM's UTC-aware
            # datetime type, so naive values must be re-stamped (SQLite
            # returns the stored string, SQL Server a naive datetime).
            created = row.created_at
            if created is not None:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created = created.isoformat()
            episodes.append({
                "episode_id": row.episode_id,
                "conversation_id": row.conversation_id,
                "summary": row.summary,
                "key_findings": kf,
                "open_questions": oq,
                "created_at": created,
            })

        return {"episodes": episodes}

    async def _op_update(self, params: dict, context: ToolContext) -> dict:
        memory_id = params.get("memory_id")
        if not memory_id:
            return {"error": "memory_id is required for update"}

        from sqlalchemy import text

        result = await context.db_session.execute(
            text(
                "SELECT memory_id FROM personal_memories "
                "WHERE memory_id = :mid AND user_id = :uid "
                "AND (project_id = :pid OR project_id IS NULL)"
            ),
            {"mid": memory_id, "uid": context.user_id, "pid": context.project_id},
        )
        if not result.fetchone():
            return {"error": "Memory not found or access denied"}

        # Same secret guard as save — update must not smuggle secrets into
        # personal memory via the content field.
        guard_error = self._check_secret_guard(params.get("content"))
        if guard_error:
            return {"error": guard_error}

        updates = []
        sql_params: dict[str, Any] = {"mid": memory_id}
        if params.get("content"):
            updates.append("content = :content")
            sql_params["content"] = params["content"]
        if params.get("tags") is not None:
            updates.append("tags = :tags")
            sql_params["tags"] = json.dumps(_coerce_tags(params["tags"]))
        if not updates:
            return {"error": "Nothing to update — provide content or tags"}

        updates.append("updated_at = :now")
        sql_params["now"] = datetime.now(timezone.utc).isoformat()

        # UPDATE must repeat the ownership predicate from the SELECT
        # above — between the check and the write, another session could have
        # deleted this row (leaving a tombstone for a different user's row if
        # memory_id collided) and a bare WHERE memory_id would touch it.
        await context.db_session.execute(
            text(
                f"UPDATE personal_memories SET {', '.join(updates)} "
                "WHERE memory_id = :mid AND user_id = :uid "
                "AND (project_id = :pid OR project_id IS NULL)"
            ),
            {**sql_params, "uid": context.user_id, "pid": context.project_id},
        )
        await context.db_session.commit()

        if context.session:
            await context.session.emit("memory_saved", memory_id=memory_id, content=params.get("content", ""))

        return {"success": True, "memory_id": memory_id}

    async def _op_archive(self, params: dict, context: ToolContext) -> dict:
        memory_id = params.get("memory_id")
        if not memory_id:
            return {"error": "memory_id is required for archive"}

        from sqlalchemy import text

        result = await context.db_session.execute(
            text(
                "SELECT memory_id FROM personal_memories "
                "WHERE memory_id = :mid AND user_id = :uid "
                "AND (project_id = :pid OR project_id IS NULL)"
            ),
            {"mid": memory_id, "uid": context.user_id, "pid": context.project_id},
        )
        if not result.fetchone():
            return {"error": "Memory not found or access denied"}

        # Same ownership predicate as the SELECT above (see _op_update note).
        await context.db_session.execute(
            text(
                "UPDATE personal_memories SET is_archived = :archived "
                "WHERE memory_id = :mid AND user_id = :uid "
                "AND (project_id = :pid OR project_id IS NULL)"
            ),
            {"mid": memory_id, "uid": context.user_id, "pid": context.project_id, "archived": True},
        )
        await context.db_session.commit()

        if context.session:
            await context.session.emit("memory_archived", memory_id=memory_id)

        return {"success": True, "memory_id": memory_id, "archived": True}
