"""Conversation history compressor.

When conversation history token count exceeds a threshold, earlier messages
are summarized using model_low to stay within budget.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache

from .providers.base import LLMProvider, Message

COMPRESS_THRESHOLD_RATIO = 0.6
RECENT_TURNS_KEEP = 8
TOKENS_PER_WORD = 1.3
TOKENS_PER_CJK_CHAR = 0.6

_log = logging.getLogger("agent_core.compressor")

SUMMARY_PROMPT = """Summarize the conversation so far concisely. Preserve:
- Key facts and decisions made
- Important tool call results (file paths, URLs, data found)
- Unresolved questions or pending work
- Context needed to continue the conversation

Be factual and brief. Output only the summary, no preamble."""

_CJK_RE = re.compile(
    r"[一-鿿぀-ゟ゠-ヿ가-힣㐀-䶿　-〿]"
)


@lru_cache(maxsize=256)
def _estimate_tokens(text: str) -> int:
    cjk_chars = len(_CJK_RE.findall(text))
    cjk_tokens = int(cjk_chars * TOKENS_PER_CJK_CHAR)
    non_cjk_text = _CJK_RE.sub("", text)
    non_cjk_words = len(non_cjk_text.split())
    non_cjk_tokens = int(non_cjk_words * TOKENS_PER_WORD)
    return cjk_tokens + non_cjk_tokens


def _estimate_tokens_safe(text: str) -> int:
    """Token estimate that never caches large payloads.

    _estimate_tokens' lru_cache keyed on the FULL text retained
    tool results that can reach MBs (filesystem reads, knowledge pages) for
    the process lifetime, and the binary-search truncation path cached every
    prefix substring on top. Short texts keep the cache; long texts are
    estimated through the uncached original.
    """
    if len(text) <= 4096:
        return _estimate_tokens(text)
    return _estimate_tokens.__wrapped__(text)


def _content_as_str(content: str | list | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))


def estimate_history_tokens(messages: list[Message]) -> int:
    """Estimate total tokens in a message list."""
    return sum(_estimate_tokens_safe(_content_as_str(m.content)) for m in messages)


def split_early_recent(messages: list[Message]) -> tuple[list[Message], list[Message]]:
    """Split messages into (early, recent) for compression.

    `recent` keeps the last RECENT_TURNS_KEEP messages. The slice boundary can
    land between an assistant tool_calls message and its tool results — a tool
    message must never lead the request (the API rejects a tool message
    without a preceding assistant tool_calls message), so leading tool
    messages are popped off `recent`. Their content is already preserved in
    the summary of `early`.
    """
    early = messages[:-RECENT_TURNS_KEEP]
    recent = messages[-RECENT_TURNS_KEEP:]
    # A tool message must never lead the request (the API rejects a tool
    # message without a preceding assistant tool_calls) — and an assistant
    # tool_calls message must never dangle without its tool results (also
    # rejected). Pop the whole dangling pair back into `early` so the tool
    # content survives into the summary instead of being silently dropped.
    while recent and (
        recent[0].role == "tool"
        or (recent[0].role == "assistant" and recent[0].tool_calls)
    ):
        early.append(recent.pop(0))
    return early, recent


def format_early_history(early: list[Message]) -> str:
    """Build the summarization input text from the early segment."""
    early_text_parts = []
    for m in early:
        role_label = m.role.upper()
        content = _content_as_str(m.content)
        if m.tool_calls:
            tool_names = [tc.get("function", {}).get("name", "?") for tc in m.tool_calls if isinstance(tc, dict)]
            content += f" [tools: {', '.join(tool_names)}]"
        if content.strip():
            early_text_parts.append(f"{role_label}: {content[:2000]}")
    return "\n".join(early_text_parts)


def build_summary_pair(summary: str) -> list[Message]:
    """Build the summary marker pair replacing the compressed early segment.

    The summary uses the "user" role so it doesn't overwrite the agent system
    prompt in providers that flatten multiple system messages.
    """
    summary_msg = Message(
        role="user",
        content=f"[Earlier conversation summary — this is automated context, not a user message]\n\n{summary}",
    )
    ack_msg = Message(
        role="assistant",
        content="Understood, I have the earlier conversation context.",
    )
    return [summary_msg, ack_msg]


async def compress_history(
    messages: list[Message],
    token_budget: int,
    provider: LLMProvider,
) -> list[Message]:
    """Compress early messages into a summary if history is too long.

    Returns a new message list with:
    - A system summary message at the front (if compression happened)
    - The most recent RECENT_TURNS_KEEP messages preserved as-is
    """
    total_tokens = estimate_history_tokens(messages)
    threshold = int(token_budget * COMPRESS_THRESHOLD_RATIO)

    if total_tokens <= threshold:
        return messages

    # If too few messages to split, truncate large tool results in-place
    if len(messages) <= RECENT_TURNS_KEEP:
        truncated = []
        for m in messages:
            content = _content_as_str(m.content)
            if m.role == "tool" and _estimate_tokens_safe(content) > 2000:
                # Keep the longest prefix within the 2000-token budget.
                # _estimate_tokens is monotonic in prefix length, so binary
                # search gives an exact CJK/ASCII-aware cutoff (a fixed
                # char/word ratio would overrun on CJK-heavy content).
                lo, hi = 0, len(content)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if _estimate_tokens_safe(content[:mid]) <= 2000:
                        lo = mid
                    else:
                        hi = mid - 1
                truncated.append(Message(
                    role=m.role,
                    content=content[:lo] + "\n[... truncated ...]",
                    tool_call_id=m.tool_call_id,
                    name=m.name,
                ))
            else:
                truncated.append(m)
        return truncated

    early, recent = split_early_recent(messages)

    early_text = format_early_history(early)
    if not early_text:
        return messages

    # Generate summary using model_low
    summary_messages = [
        Message(role="system", content=SUMMARY_PROMPT),
        Message(role="user", content=f"Conversation to summarize:\n\n{early_text}"),
    ]

    try:
        response = await provider.complete(summary_messages, tools=None)
        summary = response.message.content or "Previous conversation context unavailable."
    except Exception as e:
        # Compression is a soft budget optimization, never a data-loss path: a
        # transient summarization failure (429/5xx/network) must not replace
        # the early history with a placeholder. Skip compression this turn;
        # the next turn retries with the full history intact.
        _log.warning("History summarization failed, skipping compression: %s", e, exc_info=True)
        return messages

    return build_summary_pair(summary) + recent
