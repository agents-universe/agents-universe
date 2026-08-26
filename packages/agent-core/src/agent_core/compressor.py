"""Conversation history compressor.

When conversation history token count exceeds a threshold, earlier messages
are summarized using model_low to stay within budget.
"""
from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

from .providers.base import LLMProvider, Message, ToolDefinition

COMPRESS_THRESHOLD_RATIO = 0.6
RECENT_TURNS_KEEP = 8
TOKENS_PER_WORD = 1.3
TOKENS_PER_CJK_CHAR = 0.6
# Output tokens reserved for generation when computing the history budget.
# max_tokens defaults to 128000 in every agent config; reserving it all
# against a 128k context window made the budget always <= 0 and automatic
# compression dead for gpt-4o-class models. 16384 is gpt-4o's output ceiling.
MAX_OUTPUT_RESERVE = 16384
# Hard byte ceiling for a single provider request (wire size). Corporate
# gateways reject oversized bodies with 413; the token heuristic misses
# base64 images and CJK escaping, so a byte guard is the backstop. Operators
# behind a lower gateway limit (e.g. nginx default 1MB) can lower it.
_ESCAPE_FACTOR = 1.15
_MESSAGE_OVERHEAD_BYTES = 64
_TOOL_OVERHEAD_BYTES = 64

_log = logging.getLogger("agent_core.compressor")


def _env_max_request_bytes() -> int:
    raw = os.environ.get("AGENT_MAX_REQUEST_BYTES", "")
    try:
        return int(raw) if raw else 8 * 1024 * 1024
    except ValueError:
        _log.warning("AGENT_MAX_REQUEST_BYTES is not an integer, using default")
        return 8 * 1024 * 1024


MAX_REQUEST_BYTES = _env_max_request_bytes()

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


def compression_budget(context_window: int, reserved_input_tokens: int, max_tokens: int) -> int:
    """History budget after reserving input and capped output space.

    max_tokens defaults to 128000 in every agent config — reserving it all
    against a 128k window made the budget always <= 0, silently disabling
    automatic compression for gpt-4o-class models. The output reserve is
    capped at MAX_OUTPUT_RESERVE (gpt-4o's output ceiling).
    """
    return context_window - reserved_input_tokens - min(max_tokens, MAX_OUTPUT_RESERVE)


def estimate_wire_bytes(text: str) -> int:
    """Estimate the serialized wire size of a text payload.

    HTTP JSON serialization escapes CJK chars to \\uXXXX (6 bytes each), so
    CJK-heavy payloads are far larger than their raw length — the token
    heuristic underestimates them badly. ASCII text inflates ~15% from JSON
    quotes and escapes.
    """
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int((cjk * 6 + other) * _ESCAPE_FACTOR)


def estimate_request_bytes(
    messages: list[Message],
    tool_defs: list[ToolDefinition] | None = None,
) -> int:
    """Estimate the serialized request body size (bytes on the wire).

    Deliberately conservative: counts CJK escapes, base64 image data and
    tool-call JSON — the parts the token heuristic misses. No lru_cache:
    payloads can be MBs (same reasoning as _estimate_tokens_safe). The cost
    is O(payload) per call; the provider serializes the same payload right
    after, so this is not the bottleneck.
    """
    total = 0
    for m in messages:
        total += _MESSAGE_OVERHEAD_BYTES
        content = m.content
        if isinstance(content, str):
            total += estimate_wire_bytes(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    total += estimate_wire_bytes(str(part))
                elif part.get("type") == "image":
                    # Wire form is data:{media_type};base64,{data} — the
                    # prefix is ASCII and base64 inflates 4/3 over raw bytes.
                    total += 24 + len(part.get("data") or "")
                else:
                    total += estimate_wire_bytes(part.get("text") or "")
        if m.tool_calls:
            total += estimate_wire_bytes(json.dumps(m.tool_calls, ensure_ascii=True, default=str))
        if m.tool_call_id:
            total += len(m.tool_call_id) + 16
        if m.name:
            total += len(m.name) + 16
    for t in tool_defs or []:
        total += _TOOL_OVERHEAD_BYTES
        total += estimate_wire_bytes(t.name)
        total += estimate_wire_bytes(t.description)
        total += estimate_wire_bytes(json.dumps(t.parameters, ensure_ascii=True, default=str))
    return total


def truncate_oversized_tool_messages(messages: list[Message]) -> int:
    """Truncate oversized tool messages in place; returns the count truncated.

    Caps each tool message at a 2000-token budget via binary search on the
    estimated token count (monotonic in prefix length, so the cutoff is
    CJK/ASCII-aware — a fixed char ratio would overrun on CJK-heavy content).
    Roles and ids are untouched, so assistant<->tool pairing stays intact.
    """
    truncated = 0
    for m in messages:
        if m.role != "tool":
            continue
        content = _content_as_str(m.content)
        if _estimate_tokens_safe(content) <= 2000:
            continue
        lo, hi = 0, len(content)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _estimate_tokens_safe(content[:mid]) <= 2000:
                lo = mid
            else:
                hi = mid - 1
        m.content = content[:lo] + "\n[... truncated ...]"
        truncated += 1
    return truncated


def demote_image_messages(messages: list[Message]) -> int:
    """Replace base64 image parts with lightweight text references.

    Vision blocks are the single largest irreducible line item on the wire:
    base64 inflates raw bytes 4/3 and there is no summarization path for them.
    Demoting them keeps the turn alive at the cost of vision — the agent can
    still retrieve the file with `filesystem read_file` when it needs it.
    Returns the count demoted. Only the content part under a message's list
    content is touched; roles, tool_calls and ids are preserved.
    """
    demoted = 0
    for m in messages:
        if not isinstance(m.content, list):
            continue
        parts = []
        changed = False
        for part in m.content:
            if isinstance(part, dict) and part.get("type") == "image":
                media_type = part.get("media_type", "image")
                parts.append({
                    "type": "text",
                    "text": (
                        f"[Attachment image ({media_type}) removed from context to fit the "
                        "request size limit — use filesystem read_file on its path to "
                        "inspect it if needed]"
                    ),
                })
                demoted += 1
                changed = True
            else:
                parts.append(part)
        if changed:
            m.content = parts
    return demoted


def request_byte_breakdown(
    messages: list[Message],
    tool_defs: list[ToolDefinition] | None = None,
) -> dict[str, int]:
    """Per-section wire bytes for the over-limit error message.

    Splits the estimated body into four actionable buckets so the user (or an
    operator trimming AGENT_MAX_REQUEST_BYTES) can see what dominates: the
    system prompt (which carries the in-full knowledge load), attachment image
    data, plain history text, and tool schemas.
    """
    system = 0
    images = 0
    text = 0
    tools = 0
    for m in messages:
        if isinstance(m.content, str):
            content_bytes = estimate_wire_bytes(m.content)
        else:
            content_bytes = 0
            for part in m.content:
                if not isinstance(part, dict):
                    content_bytes += estimate_wire_bytes(str(part))
                elif part.get("type") == "image":
                    images += 24 + len(part.get("data") or "")
                else:
                    content_bytes += estimate_wire_bytes(part.get("text") or "")
        if m.role == "system":
            system += content_bytes
        else:
            text += content_bytes
        if m.tool_calls:
            text += estimate_wire_bytes(json.dumps(m.tool_calls, ensure_ascii=True, default=str))
    for t in tool_defs or []:
        tools += _TOOL_OVERHEAD_BYTES
        tools += estimate_wire_bytes(t.name)
        tools += estimate_wire_bytes(t.description)
        tools += estimate_wire_bytes(json.dumps(t.parameters, ensure_ascii=True, default=str))
    return {"system": system, "images": images, "text": text, "tools": tools}


def force_compress_history(
    messages: list[Message],
    byte_limit: int,
    tool_defs: list[ToolDefinition] | None = None,
) -> str:
    """Enforce a byte ceiling on the request payload before a provider call.

    Deliberately no LLM summarization here: the post-truncation over-limit
    cases are system-prompt (knowledge), recent text or attachments — a
    summary of the early segment cannot shrink those, and skipping the LLM
    keeps this hot-path guard deterministic and free of timeout/failure
    handling. Returns "ok" (unchanged), "truncated" (tool outputs shrunk) or
    "over_limit". Image demotion is deliberately NOT here: it discards vision
    and belongs after the LLM-summarization + knowledge-demotion stages of the
    async degradation chain (agent._degrade_request), not in this cheap,
    first-line truncation pass.
    """
    if estimate_request_bytes(messages, tool_defs) <= byte_limit:
        return "ok"
    truncate_oversized_tool_messages(messages)
    if estimate_request_bytes(messages, tool_defs) <= byte_limit:
        return "truncated"
    return "over_limit"


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
    force: bool = False,
) -> list[Message]:
    """Compress early messages into a summary if history is too long.

    Returns a new message list with:
    - A system summary message at the front (if compression happened)
    - The most recent RECENT_TURNS_KEEP messages preserved as-is

    ``force`` bypasses the token-threshold check. It is set when the request
    is byte-over-limit but token-under-limit (CJK escaping, base64 images) so
    a summary still shrinks the many-message history the token heuristic
    missed. The short-history branch (<= RECENT_TURNS_KEEP) still truncates
    in place rather than invoking the LLM.
    """
    total_tokens = estimate_history_tokens(messages)
    threshold = int(token_budget * COMPRESS_THRESHOLD_RATIO)

    if not force and total_tokens <= threshold:
        return messages

    # If too few messages to split, truncate large tool results in-place
    if len(messages) <= RECENT_TURNS_KEEP:
        truncate_oversized_tool_messages(messages)
        return messages

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
