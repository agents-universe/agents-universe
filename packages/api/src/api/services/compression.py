"""Manual conversation compression — summarize early history and persist the result.

The user explicitly triggers this from the chat UI. Unlike the automatic
pre-turn compression in agent_core.agent (in-memory only), this path rewrites
the messages table: superseded early rows are deleted and replaced by a
summary marker pair, so future turns and the UI both see the compressed
history.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.compressor import (
    MAX_REQUEST_BYTES,
    RECENT_TURNS_KEEP,
    SUMMARY_PROMPT,
    build_summary_pair,
    early_history_lines,
    estimate_wire_bytes,
    split_early_recent,
)
from agent_core.providers.base import LLMProvider, Message

from api.models.conversation import Conversation, Message as DbMessage

_log = logging.getLogger("agents_universe.compression")


class CompressionError(Exception):
    """User-facing compression failure with an HTTP status code."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --- Per-conversation in-flight sharing (singleflight) ----------------------
# Two concurrent POST /compress on the same conversation both pass the
# turn-activity guard and would each rewrite history around its own snapshot,
# ending with duplicated summary pairs. A refusal (409) is not an option — a
# double-click or a slow client would surface a user-facing error, and
# concurrency gates must never break multi-user access. Instead, share the
# in-flight call: the follower awaits the leader's future and returns the
# same result. Keys are (conversation, user): the provider and API key are
# resolved from the caller's own settings, so two different users compressing
# the same conversation must NOT share one call — each runs with their own
# credentials, while repeat requests from the same user still singleflight.
_compress_inflight: dict[tuple[str, str], asyncio.Future] = {}
_compress_guard = asyncio.Lock()


async def compress_once(
    db: AsyncSession,
    conversation_id: str,
    user_id: str,
) -> dict:
    """Compress a conversation, sharing the in-flight call with concurrent
    requesters (singleflight). Never raises a concurrency error; a follower
    receives the leader's result (or its exception) as-is."""
    key = (conversation_id, user_id)
    async with _compress_guard:
        future = _compress_inflight.get(key)
        if future is not None:
            return await asyncio.shield(future)
        future = asyncio.get_running_loop().create_future()
        _compress_inflight[key] = future
    try:
        result = await compress_conversation(db, conversation_id, user_id)
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
        raise
    else:
        if not future.done():
            future.set_result(result)
        return result
    finally:
        async with _compress_guard:
            if _compress_inflight.get(key) is future:
                del _compress_inflight[key]



# Cap each embedded tool output so a big result (e.g. a wide SELECT) cannot
# balloon the summarization input (mirrors the 2000-char cell cap).
_TOOL_OUTPUT_CHARS = 2000
# Above this wire size a <=RECENT_TURNS_KEEP conversation counts as "short
# but fat" — the row-count gate alone would refuse to compress a few
# multi-MB rows that the provider gateway rejects as a 413.
SHORT_CONVERSATION_BYTES = MAX_REQUEST_BYTES // 2


def _tool_output_text(out: object) -> str:
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    if len(text) > _TOOL_OUTPUT_CHARS:
        text = text[:_TOOL_OUTPUT_CHARS] + "…"
    return text


def _to_agent_messages(row: DbMessage) -> list[Message]:
    """Convert a DB message row to agent-core Message(s).

    The in-memory agent path delivers tool results as separate `tool`
    messages, but the DB stores only the assistant row with outputs embedded
    in tool_calls[i]["output"]. Emit a `tool` message per output so the
    summarization LLM can see what tools returned — manual compression
    deletes the early rows, so an output that never reaches the summary is
    lost forever.
    """
    tool_calls = None
    if row.tool_calls:
        try:
            tool_calls = json.loads(row.tool_calls)
        except (ValueError, TypeError):
            pass
    msgs = [Message(role=row.role, content=row.content or "", tool_calls=tool_calls)]
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict) and tc.get("output") is not None:
                msgs.append(Message(role="tool", content=_tool_output_text(tc["output"])))
    return msgs


def _rows_wire_bytes(rows: list[DbMessage]) -> int:
    """Wire size of a conversation's content (text + embedded tool outputs)."""
    return sum(
        estimate_wire_bytes(row.content or "")
        + estimate_wire_bytes(row.tool_calls or "")
        for row in rows
    )


def _truncate_fat_rows(rows: list[DbMessage]) -> int:
    """Cap oversized embedded tool outputs in the DB rows; returns count rewritten.

    Non-destructive of message structure: roles, ids and sequence order are
    untouched, only oversized tool outputs (and role="tool" row contents) are
    capped at _TOOL_OUTPUT_CHARS. Mirrors the in-memory truncation in
    agent_core.compressor so a manually-triggered fix persists for future
    turns. User/assistant text is deliberately left alone — truncating it
    would visibly destroy user-authored messages.
    """
    truncated = 0
    for row in rows:
        if row.role == "tool" and (row.content or ""):
            if len(row.content) > _TOOL_OUTPUT_CHARS:
                row.content = row.content[:_TOOL_OUTPUT_CHARS] + "…"
                truncated += 1
            continue
        if not row.tool_calls:
            continue
        try:
            tcs = json.loads(row.tool_calls)
        except (ValueError, TypeError):
            continue
        changed = False
        for tc in tcs if isinstance(tcs, list) else []:
            if not isinstance(tc, dict) or tc.get("output") is None:
                continue
            text = tc["output"] if isinstance(tc["output"], str) else json.dumps(tc["output"], ensure_ascii=False)
            if len(text) > _TOOL_OUTPUT_CHARS:
                tc["output"] = text[:_TOOL_OUTPUT_CHARS] + "…"
                changed = True
        if changed:
            row.tool_calls = json.dumps(tcs, ensure_ascii=False)
            truncated += 1
    return truncated


async def _resolve_provider(db: AsyncSession, user_id: str):
    """Pick the user's model for summarization and build a provider.

    Resolution order mirrors episodic_service: first UserModelConfig with an
    API key → system default → legacy user_api_keys. Raises CompressionError
    (400) when no usable model exists.
    """
    from api.config import get_settings
    from api.models.user import UserApiKey, UserModelConfig, UserTierModel
    from api.services.token_vault import decrypt_or_none

    settings = get_settings()

    config_result = await db.execute(
        select(UserModelConfig)
        .where(
            UserModelConfig.user_id == user_id,
            UserModelConfig.encrypted_key.isnot(None),
        )
        .order_by(UserModelConfig.sort_order)
        .limit(1)
    )
    mc = config_result.scalar_one_or_none()
    if mc:
        if mc.provider == "azure_openai" and not (mc.base_url or "").strip():
            raise CompressionError(400, "Azure OpenAI 模型缺少端点（Base URL），无法用于压缩。")
        api_key = decrypt_or_none(mc.encrypted_key, mc.user_id)
        if api_key is None:
            raise CompressionError(400, "存储的 API 密钥已损坏，请删除模型配置后重新保存。")
        cred: dict = {
            "api_key": api_key,
            "ssl_verify": settings.llm_ssl_verify,
            "model": mc.model_id,
        }
        if mc.context_window:
            # Per-config window override; absent = name-matched default.
            cred["context_window"] = mc.context_window
        if mc.provider == "azure_openai":
            cred["endpoint"] = (mc.base_url or "").strip()
        elif mc.base_url:
            cred["base_url"] = mc.base_url.strip()
            cred["url_mode"] = mc.url_mode
        from agent_core.providers.registry import get_provider

        return get_provider(mc.provider, cred)

    # System default fallback
    if settings.system_default_model_id and settings.system_default_api_key:
        cred = {
            "api_key": settings.system_default_api_key,
            "ssl_verify": settings.llm_ssl_verify,
            "model": settings.system_default_model_id,
        }
        if settings.system_default_base_url:
            cred["base_url"] = settings.system_default_base_url
            cred["url_mode"] = "base_url"
        from agent_core.providers.registry import get_provider

        return get_provider("openai", cred)

    # Legacy fallback: user_api_keys + user_tier_models (pre-migration users)
    legacy_result = await db.execute(
        select(UserApiKey).where(UserApiKey.user_id == user_id).limit(1)
    )
    legacy_key_row = legacy_result.scalar_one_or_none()
    if legacy_key_row:
        if legacy_key_row.provider == "azure_openai":
            raise CompressionError(400, "旧的 Azure OpenAI 凭据缺少端点，无法用于压缩。")
        api_key = decrypt_or_none(legacy_key_row.encrypted_value, user_id)
        if api_key is None:
            raise CompressionError(400, "存储的 API 密钥已损坏，请删除后重新保存。")
        tier_result = await db.execute(
            select(UserTierModel)
            .where(
                UserTierModel.user_id == user_id,
                UserTierModel.provider == legacy_key_row.provider,
            )
            .limit(1)
        )
        tier_row = tier_result.scalar_one_or_none()
        model = tier_row.model_id if tier_row else _default_model(legacy_key_row.provider)
        cred = {
            "api_key": api_key,
            "ssl_verify": settings.llm_ssl_verify,
            "model": model,
        }
        from agent_core.providers.registry import get_provider

        return get_provider(legacy_key_row.provider, cred)

    raise CompressionError(400, "未配置可用模型，无法压缩。请到 Settings → AI Models 添加模型。")


def _default_model(provider: str) -> str:
    defaults = {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
    }
    return defaults.get(provider, "gpt-4o-mini")


# Map-reduce summarization knobs for the manual path. A single complete()
# over the whole early text (2000 chars per message x up to 200 rows)
# reliably outruns any fixed timeout, so exactly the conversations that
# needed compressing could not be compressed. Chunks run bounded-concurrent.
_SUMMARY_CHUNK_CHARS = 30_000
_SUMMARY_CHUNK_CONCURRENCY = 3
_SUMMARY_TOTAL_TIMEOUT = 300.0


def _chunk_lines(lines: list[str], chunk_chars: int) -> list[str]:
    """Group whole message lines into chunks of at most chunk_chars.

    Lines are never split across chunks - a boundary mid-message drops the
    role prefix and mangles the summarization context. An oversized single
    line becomes its own chunk.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > chunk_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _complete_summary(
    provider: LLMProvider, user_text: str, system_prompt: str = SUMMARY_PROMPT
) -> str:
    """One non-streaming summarization call; returns stripped text."""
    response = await provider.complete(
        [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_text),
        ],
        tools=None,
    )
    return (response.message.content or "").strip()


async def _summarize_early(provider: LLMProvider, early_lines: list[str]) -> str:
    """Summarize the early history, map-reduce when it spans chunks.

    Each ~30k-char chunk is summarized independently (bounded concurrency),
    then the partial summaries merge into one - sequential chunks would
    multiply latency past the total timeout on long conversations.
    """
    chunks = _chunk_lines(early_lines, _SUMMARY_CHUNK_CHARS)
    if len(chunks) == 1:
        return await _complete_summary(
            provider, "Conversation to summarize:\n\n" + chunks[0]
        )
    semaphore = asyncio.Semaphore(_SUMMARY_CHUNK_CONCURRENCY)

    async def _one(i: int, chunk: str) -> str:
        async with semaphore:
            return await _complete_summary(
                provider,
                f"Conversation to summarize (part {i + 1}/{len(chunks)}):\n\n" + chunk,
            )

    partials = await asyncio.gather(*(_one(i, c) for i, c in enumerate(chunks)))
    merged = "\n\n".join(f"[Part {i + 1}]\n{p}" for i, p in enumerate(partials) if p)
    merge_prompt = (
        "You are given partial summaries of one long conversation, in order. "
        "Merge them into one concise summary. Preserve key facts and decisions, "
        "important tool call results (file paths, URLs, data found), unresolved "
        "questions, pending work, and context needed to continue. "
        "Output only the merged summary."
    )
    return await _complete_summary(
        provider, "Partial summaries to merge:\n\n" + merged, system_prompt=merge_prompt
    )


async def compress_conversation(
    db: AsyncSession,
    conversation_id: str,
    user_id: str,
) -> dict:
    """Summarize the early history with the LLM and persist the result.

    Returns {"summary", "deleted_count", "kept_count"}. The caller queries
    the remaining messages afterwards. Raises CompressionError for any
    user-facing failure; on LLM failure nothing is deleted.
    """
    result = await db.execute(
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num)
    )
    rows = result.scalars().all()

    if len(rows) <= RECENT_TURNS_KEEP:
        # The row-count gate alone would refuse to compress a few multi-MB
        # rows (embedded tool outputs) that the provider gateway rejects as
        # 413 — allow truncation when the content is genuinely fat.
        if _rows_wire_bytes(rows) <= SHORT_CONVERSATION_BYTES:
            raise CompressionError(409, "会话太短，无需压缩。")
        truncated = _truncate_fat_rows(rows)
        if not truncated:
            raise CompressionError(409, "会话内容过大，但没有可精简的工具输出。建议新建会话或减少单条消息大小。")
        # Truncation only rewrites existing rows (sequence_num untouched), so
        # no conversation row lock is needed; commit under the caller's
        # session and return additive fields the UI ignores.
        await db.commit()
        return {
            "summary": f"已精简 {truncated} 条过大的工具输出（每条保留前 {_TOOL_OUTPUT_CHARS} 字符），后续发送将使用精简后的内容。",
            "deleted_count": 0,
            "kept_count": len(rows),
            "truncated": truncated,
        }

    agent_messages: list[Message] = []
    msg_row: list[int] = []
    for i, row in enumerate(rows):
        for m in _to_agent_messages(row):
            agent_messages.append(m)
            msg_row.append(i)
    # Use split_early_recent's `early` return, NOT the raw
    # agent_messages[:-RECENT_TURNS_KEEP] slice: leading orphan tool/assistant
    # tool_calls messages are popped back into `early` and get deleted from
    # the DB below (boundary shifts up with kept_count) — they must be part of
    # the summarization input or their content is lost forever.
    early_msgs, recent_msgs = split_early_recent(agent_messages)

    # recent_msgs is a suffix of agent_messages (the split only pops from its
    # front), so its owning rows are the rows of the suffix slice. Counting
    # kept MESSAGES is wrong here: an assistant row with embedded tool
    # outputs expands to 1+N messages, so len(recent_msgs) can exceed the
    # real kept row count and rows[-kept_count - 1] lands BEFORE the true
    # boundary — rows whose content is already in the summary stay in the DB
    # (duplicated context) and the returned counts lie.
    kept_row_indices = set(msg_row[-len(recent_msgs):]) if recent_msgs else set()
    # recent_msgs can be EMPTY after the split: a trailing assistant row with
    # >= RECENT_TURNS_KEEP tool_calls and no final text expands to [assistant,
    # t1..tN], and split_early_recent pops every all-tool recent message back
    # into early. Without a fallback, boundary then lands on the LAST row's
    # sequence_num and the DELETE removes the whole history — including the
    # user's latest turn (a mid-plan Stop leaves exactly this shape). Keep at
    # least the final row; never let manual compression empty a conversation.
    if not kept_row_indices:
        kept_row_indices = {len(rows) - 1}
    kept_count = len(kept_row_indices)
    first_kept_row = min(kept_row_indices)
    boundary = rows[first_kept_row - 1].sequence_num

    # first_kept_row == 0 is unreachable (rows > RECENT_TURNS_KEEP and the
    # kept suffix holds at most RECENT_TURNS_KEEP expanded messages) — kept
    # as pure defense against a future constant change.
    if first_kept_row == 0:
        raise CompressionError(409, "会话太短，无需压缩。")

    early_lines = early_history_lines(early_msgs)
    if not early_lines:
        raise CompressionError(409, "会话没有可压缩的内容。")

    provider = await _resolve_provider(db, user_id)

    # Fail-safe: a manual compression is destructive — never delete the early
    # rows if the summary could not be produced. Summarization runs
    # map-reduce over ~30k-char chunks (see _summarize_early).
    try:
        async with asyncio.timeout(_SUMMARY_TOTAL_TIMEOUT):
            summary = await _summarize_early(provider, early_lines)
        if not summary:
            summary = "Previous conversation context unavailable."
    except Exception:
        _log.warning("Manual compression failed for conversation %s", conversation_id, exc_info=True)
        raise CompressionError(502, "压缩失败，请重试。")

    # Persist under the conversation row lock (serializes sequence_num
    # assignment, same pattern as websocket/handlers.py). The status filter
    # closes the delete race: a soft-deleted conversation still has its row,
    # and rewriting history into an invisible, unrecoverable conversation
    # would be pure data damage.
    locked_conv = (
        await db.execute(
            select(Conversation.conversation_id)
            .where(
                Conversation.conversation_id == conversation_id,
                Conversation.status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_conv is None:
        await db.rollback()
        raise CompressionError(404, "会话不存在或已删除。")
    await db.execute(
        sa_delete(DbMessage).where(
            DbMessage.conversation_id == conversation_id,
            DbMessage.sequence_num <= boundary,
        )
    )
    # The summary pair must read FIRST in history order (matching the in-memory
    # compress_history layout: [summary, ack] + recent) — shift the retained
    # rows up and insert the pair at the freed positions.
    pair = build_summary_pair(summary)
    await db.execute(
        sa_update(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .values(sequence_num=DbMessage.sequence_num + len(pair))
    )
    for i, m in enumerate(pair):
        db.add(DbMessage(
            conversation_id=conversation_id,
            role=m.role,
            content=m.content,
            sequence_num=boundary + 1 + i,
        ))
    await db.commit()

    return {
        "summary": summary,
        "deleted_count": len(rows) - kept_count,
        "kept_count": kept_count,
    }
