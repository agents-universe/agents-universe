"""On-demand conversation history for isolated delegated sub-turns.

Delegated children start with no conversation history preloaded (the brief is
self-contained); this tool lets one page prior messages back in when the brief
actually references earlier discussion. The query itself lives in
``api.services.agent_turn.load_history_page``, reached through a lazy import —
the same seam ``tools/agent_delegate.py`` uses, so agent-core keeps no
import-time dependency on the API package. The tool is injected into nested
turns only (never declarable, never visible to root turns).
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext


class LoadConversationHistoryTool(Tool):
    """Page earlier messages of this conversation on demand."""

    name = "load_conversation_history"
    prompt_hint = (
        "You start a delegated sub-task without the conversation history — call "
        "this only when the brief refers to prior context you were not given; "
        "prefer working from the brief alone."
    )
    description = (
        "Load a page of this conversation's earlier messages (oldest-first), each "
        "with sequence number, role, and producing agent; replies from other "
        "agents are prefixed [display_name]. Parameters: limit (default 20, max "
        "50), before (sequence-number cursor — pass the returned next_before to "
        "page further back), agent (optional slug filter to one agent's replies). "
        "Tool calls and thinking are omitted; long texts are truncated head+tail. "
        "Use it when the instruction references earlier discussion you cannot see."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many messages to return (1-50, default 20).",
            },
            "before": {
                "type": "integer",
                "description": (
                    "Sequence-number cursor: return messages older than this "
                    "sequence. Pass the previous page's next_before to go further "
                    "back; omit for the most recent page."
                ),
            },
            "agent": {
                "type": "string",
                "description": (
                    "Optional agent slug — return only that agent's replies."
                ),
            },
        },
    }

    async def execute(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if context.db_session is None:
            return {"error": "Conversation history is unavailable: no database session."}
        try:
            from api.services.agent_turn import load_history_page
        except ImportError:
            return {"error": "Conversation history is unavailable in this environment."}

        try:
            # The chain always ends with the running agent — that slug marks
            # which replies are "its own" (returned bare) under the attribution
            # rule shared with _load_history.
            chain = tuple(getattr(getattr(context, "delegation", None), "chain", ()) or ())
            viewer = chain[-1] if chain else None

            limit = max(1, min(int(params.get("limit") or 20), 50))
            before_raw = params.get("before")
            before = int(before_raw) if before_raw not in (None, "") else None
            agent = str(params.get("agent") or "").strip() or None

            page = await load_history_page(
                context.db_session,
                context.conversation_id,
                viewer_agent_slug=viewer,
                limit=limit,
                before=before,
                agent=agent,
            )
            if page["count"] == 0:
                page["note"] = (
                    "No messages matched — the conversation may be new, the "
                    "filter matched nothing, or the cursor is past the start."
                )
            return page
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            # The tool never raises into the agent loop: a failed history read
            # is degraded context, not a failed turn.
            return {"error": f"{type(exc).__name__}: {exc}"}
