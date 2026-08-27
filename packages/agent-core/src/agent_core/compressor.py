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
# Total cap on the summarization input text (auto path). A non-streaming
# complete() over an unbounded input (2000 chars/message x up to 200
# messages) reliably outruns the caller's asyncio timeout, so compression
# degraded to "keep recent only" every turn on exactly the conversations
# that needed it. 60k chars is <= ~36k tokens - small enough to summarize
# well inside the timeout, big enough to carry the recent arc.
SUMMARY_INPUT_MAX_CHARS = 60_000
# Marker prefixing the summary user message built by build_summary_pair.
# agent._degrade_request checks it to skip re-summarizing an already
# summarized history (a summary of a summary drifts the context).
SUMMARY_MARKER = "[Earlier conversation summary"
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
    if text.isascii():
        return int(len(text.split()) * TOKENS_PER_WORD)
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
    if text.isascii():
        # ASCII payloads need no CJK scan: this runs over MB-scale tool
        # outputs on every byte-guard call, and the regex was the dominant
        # per-call cost for ASCII-heavy content.
        return int(len(text) * _ESCAPE_FACTOR)
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int((cjk * 6 + other) * _ESCAPE_FACTOR)


def _tool_calls_wire_bytes(tool_calls: list) -> int:
    """Wire estimate of a tool_calls block without json.dumps-ing it whole.

    The arguments strings are the bulk; dumping the entire block on every
    estimate call re-escaped MBs of CJK for a threshold number nobody looks
    at. Per-part estimates keep the same conservative margin (the 1.15
    escape factor covers the JSON keys/braces no longer counted).
    """
    total = 32
    for tc in tool_calls:
        if not isinstance(tc, dict):
            total += 64
            continue
        total += 32
        total += estimate_wire_bytes(str(tc.get("id") or ""))
        fn = tc.get("function")
        if isinstance(fn, dict):
            total += estimate_wire_bytes(str(fn.get("name") or ""))
            args = fn.get("arguments")
            total += estimate_wire_bytes(
                args if isinstance(args, str)
                else json.dumps(args or {}, ensure_ascii=True, default=str)
            )
        else:
            total += estimate_wire_bytes(str(fn))
    return total


def estimate_request_bytes(
    messages: list[Message],
    tool_defs: list[ToolDefinition] | None = None,
    tool_wire_bytes: int | None = None,
) -> int:
    """Estimate the serialized request body size (bytes on the wire).

    Deliberately conservative: counts CJK escapes, base64 image data and
    tool-call JSON — the parts the token heuristic misses. No lru_cache:
    payloads can be MBs (same reasoning as _estimate_tokens_safe). The cost
    is O(payload) per call; the provider serializes the same payload right
    after, so this is not the bottleneck.

    tool_wire_bytes skips re-estimating tool_defs (their schemas are static
    within a turn): the per-iteration byte guard in the agent loops passes
    the value computed once before the loop.
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
            total += _tool_calls_wire_bytes(m.tool_calls)
        if m.tool_call_id:
            total += len(m.tool_call_id) + 16
        if m.name:
            total += len(m.name) + 16
    if tool_wire_bytes is not None:
        return total + tool_wire_bytes
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
            text += _tool_calls_wire_bytes(m.tool_calls)
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
    tool_wire_bytes: int | None = None,
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
    if estimate_request_bytes(messages, tool_defs, tool_wire_bytes) <= byte_limit:
        return "ok"
    truncate_oversized_tool_messages(messages)
    if estimate_request_bytes(messages, tool_defs, tool_wire_bytes) <= byte_limit:
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


def early_history_lines(early: list[Message]) -> list[str]:
    """Per-message formatted lines of the early segment (2000-char cap each).

    Shared building block: format_early_history joins them into the auto
    summarization input; the manual compression service chunks them for its
    map-reduce pass.
    """
    lines = []
    for m in early:
        role_label = m.role.upper()
        content = _content_as_str(m.content)
        if m.tool_calls:
            tool_names = [tc.get("function", {}).get("name", "?") for tc in m.tool_calls if isinstance(tc, dict)]
            content += f" [tools: {', '.join(tool_names)}]"
        if content.strip():
            lines.append(f"{role_label}: {content[:2000]}")
    return lines


def format_early_history(early: list[Message], max_chars: int | None = None) -> str:
    """Build the summarization input text from the early segment.

    max_chars caps the TOTAL input: the summarization call is non-streaming,
    and an unbounded input (2000 chars per message x up to 200 loaded
    messages) reliably outruns the caller's asyncio timeout, so compression
    degraded to "keep recent only" on exactly the conversations that needed
    it. When over the cap the TAIL (most recent) is kept - recency matters
    most for continuation - and the drop is marked explicitly.
    """
    parts = early_history_lines(early)
    if max_chars is not None:
        total = sum(len(p) + 1 for p in parts)
        if total > max_chars:
            kept: list[str] = []
            size = 0
            for part in reversed(parts):
                # Always keep the most recent line even if it alone busts the
                # cap - an empty summary input would disable compression.
                if kept and size + len(part) + 1 > max_chars:
                    break
                kept.append(part)
                size += len(part) + 1
            kept.reverse()
            dropped = len(parts) - len(kept)
            return (
                f"[... {dropped} earlier messages omitted from the summary input "
                f"to stay within {max_chars} characters ...]" + "\n" + "\n".join(kept)
            )
    return "\n".join(parts)


def build_summary_pair(summary: str) -> list[Message]:
    """Build the summary marker pair replacing the compressed early segment.

    The summary uses the "user" role so it doesn't overwrite the agent system
    prompt in providers that flatten multiple system messages.
    """
    summary_msg = Message(
        role="user",
        content=f"{SUMMARY_MARKER} - this is automated context, not a user message]\n\n{summary}",
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
    in place rather than invoking the LLM. The summarization input is capped
    at SUMMARY_INPUT_MAX_CHARS so the call completes well inside the
    caller's asyncio timeout.
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

    early_text = format_early_history(early, max_chars=SUMMARY_INPUT_MAX_CHARS)
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
