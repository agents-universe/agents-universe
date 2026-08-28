"""Shared model-credential loading for agent turns.

The WebSocket turn handler and the published-agent (SSE) path both need the
same thing: decrypted per-user model credentials with tier metadata, and —
for published runs — the publisher's fixed model config used regardless of
the actor. Extraction is behavior-preserving (same decryption, same
fallbacks, same error surfaces); the one addition is ``fixed_config_id``,
which pins the run to exactly one config and disables complexity-based auto
routing.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("agents_universe.credentials")


async def load_model_credentials(
    db,
    user_id: str,
    *,
    fixed_config_id: str | None = None,
    ssl_verify: bool = True,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str], str | None]:
    """Load decrypted model credentials for *user_id*.

    Returns ``(credentials, tier_models, tier_map, fixed_config_id)``:

    - ``credentials``: ``{config_id: {api_key, ...}}`` — decrypted keys live
      only in memory, never logged.
    - ``tier_models``: ``{config_id: {provider, model}}`` for routing.
    - ``tier_map``: ``{complexity_tier: config_id}`` first-wins by
      ``sort_order`` — only configs that decrypted successfully enter the
      map, so auto routing never targets an unusable key.
    - ``fixed_config_id``: the resolved pin when it is present and usable;
      ``"azure_endpoint_required"`` / ``"model_config_unavailable"`` when the
      pinned config cannot be used (the caller turns these into a concrete
      error message); None when no pin was requested.
    """
    from sqlalchemy import select

    from api.models.user import UserModelConfig
    from api.services.token_vault import decrypt

    credentials: dict[str, dict] = {}
    tier_models: dict[str, dict] = {}
    # {tier: config_id} first-wins by sort_order — only configs that
    # decrypted successfully enter the map, so auto routing never targets an
    # unusable key.
    tier_map: dict[str, str] = {}
    azure_configs_missing_endpoint: set[str] = set()

    def _build_cred(provider: str, plain: str, base_url: str | None = None, model_id: str | None = None, url_mode: str = "base_url", context_window: int | None = None) -> dict:
        cred: dict = {"api_key": plain, "ssl_verify": ssl_verify}
        if provider == "azure_openai":
            cred["endpoint"] = (base_url or "").strip()
        elif base_url:
            cred["base_url"] = base_url.strip()
            cred["url_mode"] = url_mode
        if model_id:
            cred["model"] = model_id
        if context_window:
            # Per-config window override; absent = name-matched default.
            cred["context_window"] = context_window
        return cred

    # Primary source: user_model_configs table
    configs_result = await db.execute(
        select(UserModelConfig)
        .where(UserModelConfig.user_id == user_id)
        .order_by(UserModelConfig.sort_order)
    )
    for mc in configs_result.scalars().all():
        if mc.encrypted_key:
            try:
                plain = decrypt(mc.encrypted_key, mc.user_id)
                if mc.provider == "azure_openai" and not (mc.base_url or "").strip():
                    azure_configs_missing_endpoint.add(mc.config_id)
                    continue
                credentials[mc.config_id] = _build_cred(mc.provider, plain, base_url=mc.base_url, model_id=mc.model_id, url_mode=mc.url_mode, context_window=mc.context_window)
                tier_models[mc.config_id] = {"provider": mc.provider, "model": mc.model_id}
                if mc.complexity_tier in ("low", "mid", "high") and mc.complexity_tier not in tier_map:
                    tier_map[mc.complexity_tier] = mc.config_id
            except Exception:
                _log.warning("Failed to decrypt model config %s for user %s", mc.config_id, user_id, exc_info=True)

    # Legacy fallback: user_api_keys + user_tier_models (for pre-migration users)
    if not tier_models:
        from api.models.user import UserApiKey, UserToken, UserTierModel
        _LLM_PROVIDERS = {"anthropic", "openai", "azure_openai", "google_gemini"}
        keys_result = await db.execute(
            select(UserApiKey).where(UserApiKey.user_id == user_id)
        )
        for uk in keys_result.scalars().all():
            try:
                if uk.provider == "azure_openai" and not (uk.base_url or "").strip():
                    continue
                plain = decrypt(uk.encrypted_value, uk.user_id)
                # Preserve legacy user-owned endpoints until the user migrates
                # this credential to UserModelConfig; never fall back to a
                # shared system endpoint.
                credentials[uk.provider] = _build_cred(
                    uk.provider, plain, base_url=uk.base_url
                )
            except Exception:
                _log.warning("Failed to decrypt legacy API key for provider %s, user %s", uk.provider, user_id, exc_info=True)
        if len(credentials) < len(_LLM_PROVIDERS):
            legacy_result = await db.execute(
                select(UserToken).where(UserToken.user_id == user_id)
            )
            for tok in legacy_result.scalars().all():
                provider = tok.service_key.split(":", 1)[0]
                if provider in _LLM_PROVIDERS and provider not in credentials:
                    try:
                        if provider == "azure_openai" and not (tok.base_url or "").strip():
                            continue
                        plain = decrypt(tok.encrypted_value, tok.user_id)
                        # Preserve legacy user-owned endpoints until the user migrates
                        # this credential to UserModelConfig; never fall back to a
                        # shared system endpoint.
                        credentials[provider] = _build_cred(
                            provider, plain, base_url=tok.base_url, model_id=tok.model_id
                        )
                    except Exception:
                        _log.warning("Failed to decrypt legacy token for provider %s, user %s", provider, user_id, exc_info=True)
        tiers_result = await db.execute(
            select(UserTierModel).where(UserTierModel.user_id == user_id)
        )
        for tm in tiers_result.scalars().all():
            if tm.tier in credentials:
                tier_models[tm.tier] = {"provider": tm.provider, "model": tm.model_id}

    # System default fallback
    from api.config import get_settings as _get_settings
    _settings = _get_settings()
    _sys_default_id = "system-default"
    if _settings.system_default_model_id and _settings.system_default_api_key:
        tier_models[_sys_default_id] = {"provider": "openai", "model": _settings.system_default_model_id}
        credentials[_sys_default_id] = _build_cred(
            "openai", _settings.system_default_api_key,
            base_url=_settings.system_default_base_url or None,
            model_id=_settings.system_default_model_id,
        )

    # Pinned config resolution happens after all sources loaded: the pin must
    # be honored over auto routing. The caller maps the failure to a concrete
    # message via the error hint below.
    if fixed_config_id is not None:
        if fixed_config_id in azure_configs_missing_endpoint:
            return credentials, tier_models, tier_map, "azure_endpoint_required"
        if fixed_config_id not in tier_models or fixed_config_id not in credentials:
            return credentials, tier_models, tier_map, "model_config_unavailable"
        return credentials, tier_models, tier_map, fixed_config_id
    return credentials, tier_models, tier_map, None
