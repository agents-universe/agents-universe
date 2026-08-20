"""Episodic memory generation — summarizes completed conversations."""
from __future__ import annotations

import json
import logging

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from api.database import AsyncSessionLocal
from api.models.conversation import Conversation, Message as DbMessage
from api.models.memory import EpisodicMemory

_log = logging.getLogger("agents_universe.episodic")

EPISODIC_SYSTEM_PROMPT = """\
You are a conversation summarizer. Given a conversation between a user and an AI agent, produce a JSON object with:
- "summary": A concise 2-4 sentence summary of what was discussed and accomplished.
- "key_findings": An array of 2-5 key facts, decisions, or outcomes from the conversation.
- "open_questions": An array of 0-3 unresolved questions or next steps identified.

Respond ONLY with valid JSON, no markdown fences."""

MIN_USER_MESSAGES = 3


def _bounded_json_list(items: object, limit: int = 2000) -> str:
    """Serialize a list to JSON that ALWAYS parses, kept under *limit* bytes.

    Never slice the serialized output — cutting inside an escape sequence or
    mid-multibyte produces an unterminated document and json.loads() crashes
    on every read of the stored value. Instead each element is truncated and
    the tail of the list is dropped until the whole document fits.
    """
    if not isinstance(items, list):
        return "[]"
    out = ""
    for item in items:
        chunk = json.dumps(item, ensure_ascii=False)
        if len(chunk) > limit // 2:
            chunk = json.dumps(str(item)[: limit // 2], ensure_ascii=False)
        candidate = f"[{out}{', ' if out else ''}{chunk}]"
        if len(candidate) > limit:
            break
        out = f"{out}{', ' if out else ''}{chunk}"
    return f"[{out}]"


async def generate_episodic_summary(conversation_id: str, user_id: str) -> None:
    """Generate and persist an episodic summary for a conversation.

    Runs as a fire-and-forget background task. Silently returns if conditions
    are not met (too few messages, episode already exists, etc.).
    """
    async with AsyncSessionLocal() as db:
        # Check if episode already exists
        existing = await db.execute(
            select(EpisodicMemory.episode_id).where(
                EpisodicMemory.conversation_id == conversation_id,
            )
        )
        if existing.scalar_one_or_none():
            return

        # Get conversation metadata (project_id)
        conv_row = await db.execute(
            select(Conversation.project_id).where(
                Conversation.conversation_id == conversation_id,
            )
        )
        project_id = conv_row.scalar_one_or_none()
        if not project_id:
            return

        # Count user messages
        count_result = await db.execute(
            select(func.count()).select_from(DbMessage).where(
                DbMessage.conversation_id == conversation_id,
                DbMessage.role == "user",
            )
        )
        user_msg_count = count_result.scalar() or 0
        if user_msg_count < MIN_USER_MESSAGES:
            return

        # Load the most RECENT messages: sequence_num asc + limit would read
        # the OLDEST 50 (and _format_conversation's [-30:] slice then drops
        # the newest end) — a long conversation's summary would skip the
        # actual outcome. Same desc+reverse last-N pattern as list_messages.
        msg_result = await db.execute(
            select(DbMessage)
            .where(DbMessage.conversation_id == conversation_id)
            .order_by(DbMessage.sequence_num.desc())
            .limit(50)
        )
        messages = list(msg_result.scalars().all())
        messages.reverse()  # back to chronological order for formatting
        if not messages:
            return

        # Format for summarization
        formatted = _format_conversation(messages)

        # Call LLM (use lowest-tier model)
        try:
            summary_data = await _call_llm_for_summary(formatted, user_id, db)
        except Exception:
            _log.warning("LLM call failed for episodic summary", exc_info=True)
            return

        if not summary_data:
            return

        # Persist
        from api.models._compat import new_uuid

        episode = EpisodicMemory(
            episode_id=new_uuid(),
            conversation_id=conversation_id,
            user_id=user_id,
            project_id=project_id,
            # Coerce: the LLM may return null or a non-string for "summary"
            # (schema drift between providers), and slicing a non-str would
            # TypeError out of this fire-and-forget path.
            summary=str(summary_data.get("summary") or "")[:4000],
            key_findings=_bounded_json_list(summary_data.get("key_findings", [])),
            open_questions=_bounded_json_list(summary_data.get("open_questions", [])),
            generated_by="auto",
        )
        db.add(episode)
        try:
            await db.commit()
        except IntegrityError:
            # The pre-check raced with a concurrent generation task and the
            # unique constraint fired. The first summary wins.
            await db.rollback()
            return
        _log.info("Episodic summary generated for conversation %s", conversation_id)


def _format_conversation(messages: list) -> str:
    """Format DB messages into a readable transcript."""
    lines = []
    for msg in messages:
        role = msg.role.capitalize()
        content = (msg.content or "")[:1000]
        if content:
            lines.append(f"{role}: {content}")
    return "\n\n".join(lines[-30:])


async def _call_llm_for_summary(transcript: str, user_id: str, db) -> dict | None:
    """Call the lowest-tier LLM to generate a summary. Returns parsed JSON or None."""
    from api.models.user import UserModelConfig

    # Find user's first available model config with an API key
    result = await db.execute(
        select(UserModelConfig).where(
            UserModelConfig.user_id == user_id,
            UserModelConfig.encrypted_key.isnot(None),
        ).order_by(UserModelConfig.sort_order).limit(1)
    )
    config_row = result.scalar_one_or_none()

    import httpx
    from api.services.token_vault import decrypt
    from api.config import get_settings

    settings = get_settings()

    custom_base_url: str | None = None
    if config_row:
        api_key = decrypt(config_row.encrypted_key, user_id)
        provider = config_row.provider
        model = config_row.model_id
        custom_base_url = config_row.base_url
    elif settings.system_default_model_id and settings.system_default_api_key:
        api_key = settings.system_default_api_key
        provider = "openai"
        model = settings.system_default_model_id
        custom_base_url = settings.system_default_base_url or None
    else:
        # Legacy fallback: try user_api_keys + user_tier_models
        from api.models.user import UserApiKey, UserTierModel
        legacy_key_result = await db.execute(
            select(UserApiKey).where(UserApiKey.user_id == user_id).limit(1)
        )
        legacy_key_row = legacy_key_result.scalar_one_or_none()
        if not legacy_key_row:
            _log.debug("No model config for user %s, skipping episodic generation", user_id)
            return None
        api_key = decrypt(legacy_key_row.encrypted_value, user_id)
        provider = legacy_key_row.provider
        if provider == "azure_openai":
            _log.debug("Legacy Azure API key has no model-config endpoint; skipping episodic generation")
            return None
        # Legacy keys are credentials only; custom endpoints belong to UserModelConfig.
        custom_base_url = None
        tier_result = await db.execute(
            select(UserTierModel).where(
                UserTierModel.user_id == user_id,
                UserTierModel.provider == provider,
            ).limit(1)
        )
        tier_row = tier_result.scalar_one_or_none()
        model = tier_row.model_id if tier_row else _default_model(provider)

    # Same SSRF gate as routers/model_configs.py `_do_test` — custom_base_url is
    # user-controlled and this function POSTs the decrypted API key to it. The
    # guard is always-on for literal IPs/metadata hosts (port allowlist NOT
    # applied: self-hosted gateways on arbitrary ports are legitimate), DNS
    # resolution follows SSRF_ENABLED. A raised SSRFError is caught by the
    # caller and the episode is silently skipped (fire-and-forget task).
    if custom_base_url:
        # resolve_and_validate directly — validate_outbound_url would
        # re-apply the port allowlist and defeat allow_any_port .
        from agent_core.tools._ssrf import validate_url as _ssrf_validate_url
        _ssrf_validate_url(custom_base_url, allow_any_port=True)
        from agent_core.tools._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from agent_core.tools._ssrf import resolve_and_validate
            from urllib.parse import urlparse
            _p = urlparse(custom_base_url)
            resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))

    messages_payload = [
        {"role": "system", "content": EPISODIC_SYSTEM_PROMPT},
        {"role": "user", "content": f"Summarize this conversation:\n\n{transcript}"},
    ]

    try:
        async with httpx.AsyncClient(timeout=30.0, verify=settings.llm_ssl_verify) as client:
            if provider == "anthropic":
                anthropic_url = (custom_base_url or "https://api.anthropic.com").rstrip("/")
                resp = await client.post(
                    f"{anthropic_url}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "system": EPISODIC_SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": f"Summarize this conversation:\n\n{transcript}"}],
                    },
                )
                content_path = ("content", 0, "text")
            elif provider == "azure_openai":
                if not custom_base_url:
                    _log.debug("Azure model config has no endpoint; skipping episodic generation")
                    return None
                endpoint = custom_base_url.rstrip("/")
                resp = await client.post(
                    f"{endpoint}/openai/deployments/{model}/chat/completions?api-version=2024-08-01-preview",
                    headers={"api-key": api_key, "content-type": "application/json"},
                    json={"max_completion_tokens": 500, "messages": messages_payload},
                )
                content_path = ("choices", 0, "message", "content")
            elif provider == "google_gemini":
                endpoint = (custom_base_url or "https://generativelanguage.googleapis.com").rstrip("/")
                # Key goes in the x-goog-api-key header, never in the URL —
                # query params end up in request logs and httpx exception reprs.
                resp = await client.post(
                    f"{endpoint}/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key, "content-type": "application/json"},
                    json={
                        "systemInstruction": {"parts": [{"text": EPISODIC_SYSTEM_PROMPT}]},
                        "contents": [{"role": "user", "parts": [{"text": f"Summarize this conversation:\n\n{transcript}"}]}],
                        "generationConfig": {"maxOutputTokens": 500},
                    },
                )
                content_path = ("candidates", 0, "content", "parts", 0, "text")
            elif provider == "openai":
                base_url = (custom_base_url or "https://api.openai.com").rstrip("/")
                if not base_url.endswith("/v1"):
                    base_url = f"{base_url}/v1"
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "max_tokens": 500, "messages": messages_payload},
                )
                content_path = ("choices", 0, "message", "content")
            else:
                _log.debug("Unsupported episodic summary provider %s", provider)
                return None

            if resp.status_code != 200:
                _log.warning("LLM API returned %d for episodic generation", resp.status_code)
                return None

            data = resp.json()
            content_text = data
            for key in content_path:
                content_text = content_text[key]
            return json.loads(content_text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        _log.warning("Failed to parse episodic LLM response: %s", e)
        return None
    except Exception:
        _log.warning("HTTP error during episodic generation", exc_info=True)
        return None


def _default_model(provider: str) -> str:
    defaults = {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
        "azure": "gpt-4o-mini",
    }
    return defaults.get(provider, "gpt-4o-mini")
