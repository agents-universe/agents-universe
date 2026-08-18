"""SQL query tool — read-only queries + special server operations."""
from __future__ import annotations

import logging
import re
from typing import Any

from .base import Tool, ToolContext

_log = logging.getLogger(__name__)

# Cap on rows/cell size returned to the agent  — SELECT * against a
# large table must not balloon the LLM context or agent memory.
_MAX_ROWS = 500
_MAX_CELL_CHARS = 2000

# Special operations that are not raw SQL
_SPECIAL_OPS = {"reindex_knowledge"}

# Keywords that must never appear in a read-only query (word-boundary match)
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE|EXEC|EXECUTE|INTO|OPENROWSET|OPENQUERY|xp_\w+|sp_\w+)\b",
    re.IGNORECASE,
)


def _strip_comments(query: str) -> str:
    """Remove -- and /* */ comments, preserving string literals.

    Comment markers inside '...' literals are data, not comments — a naive
    regex strip truncates ``SELECT 'a--b'`` (syntax error) and silently
    corrupts ``SELECT 'a/*b*/c'`` (wrong data returned to the agent). A state
    machine tracks the literal state (with '' escapes) and SQL Server bracket
    identifiers, and only strips comments outside them.
    """
    out: list[str] = []
    i, n = 0, len(query)
    in_literal = False
    while i < n:
        ch = query[i]
        if in_literal:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and query[i + 1] == "'":
                    out.append(query[i + 1])  # escaped '' inside the literal
                    i += 1
                else:
                    in_literal = False
            i += 1
        elif ch == "'":
            in_literal = True
            out.append(ch)
            i += 1
        elif ch == "[":
            # SQL Server quoted identifier ([weird--name]) — keep verbatim
            out.append(ch)
            i += 1
            while i < n:
                out.append(query[i])
                if query[i] == "]":
                    if i + 1 < n and query[i + 1] == "]":
                        out.append(query[i + 1])  # escaped ]] inside the name
                        i += 1
                    else:
                        i += 1
                        break
                i += 1
        elif ch == "-" and i + 1 < n and query[i + 1] == "-":
            out.append(" ")  # line comment → whitespace
            while i < n and query[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and query[i + 1] == "*":
            out.append(" ")  # block comment → whitespace
            i += 2
            while i + 1 < n and not (query[i] == "*" and query[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _mask_string_literals(query: str) -> str:
    """Blank out the contents of '...' literals, keeping the quotes.

    The keyword/table blocklists must not trip on data inside literals
    (``SELECT 'DROP TABLE'`` is a harmless read) — but they must still see
    real identifiers, which never appear inside quotes. Masking the checked
    text is deterministic and leaves the executed text untouched, so
    validate-and-execute-the-same-text still holds.
    """
    out: list[str] = []
    i, n = 0, len(query)
    in_literal = False
    while i < n:
        ch = query[i]
        if in_literal:
            if ch == "'":
                if i + 1 < n and query[i + 1] == "'":
                    out.append("  ")  # escaped '' stays inside the literal
                    i += 2
                else:
                    out.append(ch)
                    in_literal = False
                    i += 1
            else:
                out.append(" ")
                i += 1
        elif ch == "'":
            in_literal = True
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)

# Tables that contain encrypted credentials or sensitive user data.
# Queries referencing these are blocked to prevent token/secret exfiltration.
_BLOCKED_TABLES = re.compile(
    r"\b(user_tokens|user_api_keys|project_secrets|personal_memories)\b",
    re.IGNORECASE,
)


def _validate_readonly_select(query: str) -> tuple[str | None, str]:
    """Return (error_message, stripped_query). error_message is None when
    the stripped query is a single read-only SELECT.

    The stripped text is BOTH validated and executed — validating a
    comment-stripped copy while executing the original let comments split
    forbidden keywords/table names (``user/*c*/_tokens``) in the executed
    text while the checked copy no longer matched.
    """
    # Strip comments so keywords can't hide inside them. String literals are
    # preserved — a comment marker inside '...' is data, not a comment.
    stripped = _strip_comments(query).strip()
    if not stripped:
        return "Empty query", stripped
    if not stripped.upper().startswith(("SELECT", "WITH")):
        return "Only SELECT queries are allowed. Use 'operation' for server operations.", stripped
    # Reject statement batching: no semicolons except one optional trailing.
    # Run on the literal-masked text — a ';' inside a string is data, not a
    # statement separator (`SELECT 'a;b'` is a single legit read).
    if ";" in _mask_string_literals(stripped).rstrip().rstrip(";"):
        return "Multiple SQL statements are not allowed.", stripped
    match = _FORBIDDEN_KEYWORDS.search(_mask_string_literals(stripped))
    if match:
        return f"Forbidden keyword in read-only query: {match.group(0)}", stripped
    table_match = _BLOCKED_TABLES.search(_mask_string_literals(stripped))
    if table_match:
        return f"Access to credential table is blocked: {table_match.group(0)}", stripped
    return None, stripped


class SqlQueryTool(Tool):
    name = "sql_query"
    prompt_hint = (
        "Read-only SELECT queries against the application database. Writes and DDL are "
        "rejected by design and credential tables are blocked — do not try to work "
        "around a rejection."
    )
    description = (
        "Run read-only SQL SELECT queries against the application database, "
        "or trigger special server operations like 'reindex_knowledge'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "SQL SELECT statement, OR a special operation name",
            },
            "operation": {
                "type": "string",
                "enum": list(_SPECIAL_OPS),
                "description": "Special server-side operation (alternative to query)",
            },
            "params": {
                "type": "object",
                "description": "Parameters for the query or operation",
            },
        },
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        operation = params.get("operation")
        query = params.get("query", "")
        op_params = params.get("params", {})

        # Handle special operations
        if operation == "reindex_knowledge" or query == "reindex_knowledge":
            return await self._reindex_knowledge(op_params, context)

        # Validate it's a single read-only SELECT (safety check)
        validation_error, stripped_query = _validate_readonly_select(query)
        if validation_error:
            return {"error": validation_error}

        if context.db_session is None:
            return {"error": "No database session available"}

        try:
            from sqlalchemy import text
            # Execute the validated (comment-stripped) text, not the raw query —
            # validating one text and executing another is a bypass in itself.
            result = await context.db_session.execute(text(stripped_query), op_params)
            # an unbounded SELECT (`SELECT * FROM huge_table`) pulled
            # the whole table into memory and into the LLM context. Cap the
            # rows returned and truncate oversized cell values.
            rows: list[dict] = []
            truncated = False
            for i, row in enumerate(result):
                if i >= _MAX_ROWS:
                    truncated = True
                    break
                row_dict: dict = {}
                for k, v in row._mapping.items():
                    if isinstance(v, str) and len(v) > _MAX_CELL_CHARS:
                        row_dict[k] = v[:_MAX_CELL_CHARS] + "…"
                    else:
                        row_dict[k] = v
                rows.append(row_dict)
            out = {"rows": rows, "row_count": len(rows), "truncated": truncated}
            if truncated:
                out["warning"] = f"Results truncated at {_MAX_ROWS} rows — add a WHERE/LIMIT to narrow the query."
            return out
        except Exception as e:
            _log.warning("sql_query failed: %s | query: %.200s", e, query)
            return {"error": f"Query failed: {e}"}

    async def _reindex_knowledge(self, op_params: dict, context: ToolContext) -> dict:
        """Trigger reindex of knowledge. Without fs_path, reindexes entire project knowledge directory."""
        from pathlib import Path

        fs_path = op_params.get("fs_path")

        if context.db_session is None:
            return {"error": "No database session available"}

        if not fs_path:
            try:
                from agent_core.knowledge.index import index_directory

                knowledge_dir = Path(context.knowledge_dir())
                if not knowledge_dir.exists():
                    return {"error": f"Knowledge directory not found: {knowledge_dir}"}
                stats = await index_directory(
                    directory=knowledge_dir,
                    project_id=context.project_id,
                    db_session=context.db_session,
                )
                await context.db_session.commit()
                return {"success": True, "stats": stats}
            except Exception as e:
                _log.warning("reindex_knowledge (full) failed: %s", e, exc_info=True)
                return {"error": f"Reindex failed: {e}"}

        try:
            from agent_core.knowledge.index import reindex_one

            knowledge_dir = Path(context.knowledge_dir()).resolve()
            path = Path(fs_path)
            if not path.is_absolute():
                path = knowledge_dir / fs_path
            path = path.resolve()
            if not path.is_relative_to(knowledge_dir):
                return {"error": f"Access denied: path {fs_path!r} is outside knowledge directory"}
            result = await reindex_one(
                fs_path=str(path),
                project_id=context.project_id,
                db_session=context.db_session,
            )
            await context.db_session.commit()
            return result
        except Exception as e:
            _log.warning("reindex_knowledge (single) failed for %s: %s", fs_path, e, exc_info=True)
            return {"error": f"Reindex failed: {e}"}
