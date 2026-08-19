"""Complexity pre-classification for the "auto" model option.

One cheap LLM call (max_tokens=8, temperature=0) decides whether the incoming
user message is low / mid / high complexity so the turn can be routed to the
matching tier of the user's configured models. Any failure (timeout, API
error, unparseable output) degrades silently to None — the caller then falls
back to the existing default selection. Classification tokens never count
toward session.tokens_used: the classifier runs before the ConversationSession
exists.
"""
from __future__ import annotations

import asyncio
import logging
import re

_log = logging.getLogger("agents_universe.complexity")

# System prompt kept in English: the classifier output feeds a regex
# (\b(low|mid|high)\b), so the tiers must come back verbatim.
_SYSTEM_PROMPT = (
    "You are a task-complexity classifier for an AI agent platform. "
    "Given a user message, reply with exactly one word: "
    "'low' (trivial or short request), 'mid' (moderate task), "
    "or 'high' (complex multi-step task). Reply with nothing else."
)

_MAX_HISTORY_MESSAGES = 8
_MAX_HISTORY_CHARS = 4000
_CLASSIFY_TIMEOUT = 10.0


async def classify_complexity(
    db,
    conversation_id: str,
    user_message: str,
    credentials: dict[str, dict],
    tier_models: dict[str, dict],
    classifier_config_id: str,
) -> str | None:
    """Classify *user_message* as low/mid/high using the cheapest configured model.

    Returns None when the model cannot be used, the call fails, or the output
    does not parse — callers treat None as "use the default model selection".
    """
    from sqlalchemy import select

    from agent_core.providers.base import Message
    from agent_core.providers.registry import get_provider
    from api.models.conversation import Message as DbMessage

    model_cfg = tier_models.get(classifier_config_id)
    if not model_cfg:
        return None

    # Recent history (excluding this turn) gives the classifier context —
    # e.g. "continue" after a long exchange reads as high only if the
    # conversation shows it is part of a complex flow.
    query = (
        select(DbMessage)
        .where(DbMessage.conversation_id == conversation_id)
        .order_by(DbMessage.sequence_num.desc())
        .limit(_MAX_HISTORY_MESSAGES)
    )
    result = await db.execute(query)
    history_msgs = list(reversed(result.scalars().all()))

    history: list[Message] = []
    used_chars = 0
    # Accumulate newest-first (reversed history) so one oversized old message
    # can't drop everything newer; rows that don't fit are skipped. The kept
    # list is reversed back to chronological order for the prompt.
    for m in reversed(history_msgs):
        content = (m.content or "").strip()
        if not content or m.role not in ("user", "assistant"):
            continue
        if used_chars + len(content) > _MAX_HISTORY_CHARS:
            continue
        history.append(Message(role=m.role, content=content[:2000]))
        used_chars += len(content)
    history.reverse()
    history.append(Message(role="user", content=user_message[:4000]))

    # Same credential merge as Agent._get_provider: the decrypted key lives
    # only in memory (never logged), the model comes from the tier entry.
    merged = {
        **credentials.get(classifier_config_id, {}),
        **{k: v for k, v in model_cfg.items() if k != "provider"},
    }
    provider = get_provider(model_cfg.get("provider", "openai"), merged)
    try:
        async with asyncio.timeout(_CLASSIFY_TIMEOUT):
            completion = await provider.complete(
                messages=[Message(role="system", content=_SYSTEM_PROMPT), *history],
                max_tokens=8,
                temperature=0.0,
            )
        text = (completion.message.content or "") if isinstance(completion.message.content, str) else ""
        match = re.search(r"\b(low|mid|high)\b", text.lower())
        if not match:
            _log.debug(
                "Classifier returned unparseable output for conversation=%s: %r",
                conversation_id, text[:80],
            )
            return None
        return match.group(1)
    except asyncio.TimeoutError:
        _log.debug("Complexity pre-classification timed out for conversation=%s", conversation_id)
        return None
    except Exception:
        # Key revoked, provider error, network failure — never block the turn.
        _log.debug("Complexity pre-classification failed for conversation=%s", conversation_id, exc_info=True)
        return None
    finally:
        await provider.close()
