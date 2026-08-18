"""User model configuration management — supports multiple entries per provider."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("agents_universe.model_configs")

from api.config import get_settings
from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.user import UserModelConfig
from api.services.token_vault import decrypt_or_none, encrypt, key_hint as make_hint

router = APIRouter()

VALID_PROVIDERS = {"anthropic", "openai", "azure_openai", "google_gemini"}

SYSTEM_DEFAULT_CONFIG_ID = "system-default"


def _normalize_base_url(value: str | None) -> str | None:
    """Normalize and validate a user-provided provider endpoint."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an HTTP or HTTPS URL with a hostname")
    return normalized.rstrip("/")


def _validate_url_mode(value: str | None) -> str | None:
    if value is not None and value not in ("base_url", "full_url"):
        raise ValueError("url_mode must be 'base_url' or 'full_url'")
    return value


class ModelConfigCreate(BaseModel):
    provider: str = Field(max_length=50)
    model_id: str = Field(max_length=200)
    # bounded to the encrypted_key column (String(4000)): AES-GCM ciphertext
    # is plaintext+28 bytes and base64 expands it ~4/3, so 2800 chars store as
    # ~3772 chars — longer values raise DataError on MSSQL (500).
    api_key: str | None = Field(None, max_length=2800)
    base_url: str | None = Field(None, max_length=500)
    url_mode: str | None = Field(None, max_length=20)

    @field_validator("base_url")
    @classmethod
    def trim_base_url(cls, value: str | None) -> str | None:
        return _normalize_base_url(value)

    @field_validator("url_mode")
    @classmethod
    def validate_url_mode(cls, value: str | None) -> str | None:
        return _validate_url_mode(value)


class ModelConfigUpdate(BaseModel):
    model_id: str | None = Field(None, max_length=200)
    # same column-bound math as ModelConfigCreate.api_key.
    api_key: str | None = Field(None, max_length=2800)
    base_url: str | None = Field(None, max_length=500)
    url_mode: str | None = Field(None, max_length=20)

    @field_validator("base_url")
    @classmethod
    def trim_base_url(cls, value: str | None) -> str | None:
        return _normalize_base_url(value)

    @field_validator("url_mode")
    @classmethod
    def validate_url_mode(cls, value: str | None) -> str | None:
        return _validate_url_mode(value)


def _system_default_entry() -> dict | None:
    settings = get_settings()
    if not settings.system_default_model_id or not settings.system_default_api_key:
        return None
    return {
        "config_id": SYSTEM_DEFAULT_CONFIG_ID,
        "provider": "openai",
        "model_id": settings.system_default_model_id,
        "key_hint": None,
        "base_url": settings.system_default_base_url or None,
        "url_mode": "base_url",
        "is_system": True,
    }


@router.get("")
async def list_model_configs(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserModelConfig)
        .where(UserModelConfig.user_id == current_user.user_id)
        .order_by(UserModelConfig.sort_order)
    )
    rows = result.scalars().all()

    configs = [
        {
            "config_id": r.config_id,
            "provider": r.provider,
            "model_id": r.model_id,
            "key_hint": r.key_hint,
            "base_url": r.base_url,
            "url_mode": r.url_mode,
            "is_system": False,
        }
        for r in rows
    ]

    sys_default = _system_default_entry()
    if sys_default:
        configs.append(sys_default)

    return configs


@router.post("")
async def create_model_config(
    body: ModelConfigCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    if body.provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider!r}")
    if not body.model_id.strip():
        raise HTTPException(status_code=400, detail="model_id cannot be empty")
    if body.provider == "azure_openai" and not body.base_url:
        raise HTTPException(status_code=400, detail="Azure OpenAI requires a base URL")

    user_id = current_user.user_id

    # Determine sort_order (append at end)
    max_order_result = await db.execute(
        select(func.max(UserModelConfig.sort_order))
        .where(UserModelConfig.user_id == user_id)
    )
    max_val = max_order_result.scalar()
    next_order = 0 if max_val is None else max_val + 1

    encrypted = None
    hint = None
    if body.api_key:
        encrypted = encrypt(body.api_key, user_id)
        hint = make_hint(body.api_key)

    row = UserModelConfig(
        user_id=user_id,
        provider=body.provider,
        model_id=body.model_id.strip(),
        encrypted_key=encrypted,
        key_hint=hint,
        base_url=body.base_url or None,
        url_mode=body.url_mode or "base_url",
        sort_order=next_order,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    return {
        "config_id": row.config_id,
        "provider": row.provider,
        "model_id": row.model_id,
        "key_hint": row.key_hint,
        "base_url": row.base_url,
        "url_mode": row.url_mode,
        "is_system": False,
    }


@router.put("/{config_id}")
async def update_model_config(
    config_id: str,
    body: ModelConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserModelConfig).where(
            UserModelConfig.config_id == config_id,
            UserModelConfig.user_id == current_user.user_id,
        ).with_for_update()
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Model config not found")

    if body.model_id is not None:
        if not body.model_id.strip():
            raise HTTPException(status_code=400, detail="model_id cannot be empty")
        row.model_id = body.model_id.strip()

    if body.api_key is not None:
        row.encrypted_key = encrypt(body.api_key, row.user_id)
        row.key_hint = make_hint(body.api_key)

    if "base_url" in body.model_fields_set:
        row.base_url = body.base_url
    if "url_mode" in body.model_fields_set and body.url_mode:
        row.url_mode = body.url_mode
    if row.provider == "azure_openai" and not row.base_url:
        raise HTTPException(status_code=400, detail="Azure OpenAI requires a base URL")

    row.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "config_id": row.config_id,
        "provider": row.provider,
        "model_id": row.model_id,
        "key_hint": row.key_hint,
        "base_url": row.base_url,
        "url_mode": row.url_mode,
        "is_system": False,
    }


@router.delete("/{config_id}")
async def delete_model_config(
    config_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserModelConfig).where(
            UserModelConfig.config_id == config_id,
            UserModelConfig.user_id == current_user.user_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Model config not found")

    await db.delete(row)
    await db.commit()
    return {"deleted": config_id}


class TestConnectionBody(BaseModel):
    provider: str = Field(max_length=50)
    model_id: str = Field(max_length=200)
    # test-only (not persisted), but kept consistent with the saved configs.
    api_key: str = Field(max_length=2800)
    base_url: str | None = Field(None, max_length=500)
    url_mode: str | None = Field(None, max_length=20)

    @field_validator("base_url")
    @classmethod
    def trim_base_url(cls, value: str | None) -> str | None:
        return _normalize_base_url(value)

    @field_validator("url_mode")
    @classmethod
    def validate_url_mode(cls, value: str | None) -> str | None:
        return _validate_url_mode(value)


@router.post("/test-connection")
async def test_connection(
    body: TestConnectionBody,
    current_user: UserInfo = Depends(get_current_user),
):
    """Test model connectivity without saving — used by the 'Add' form."""
    if body.provider not in VALID_PROVIDERS:
        return {"ok": False, "error": f"Unknown provider: {body.provider!r}"}
    if not body.api_key:
        return {"ok": False, "error": "API key is required for testing"}
    if body.provider == "azure_openai" and not body.base_url:
        return {"ok": False, "error": "Azure OpenAI requires a base URL"}

    return await _do_test(body.provider, body.model_id, body.api_key, body.base_url, body.url_mode or "base_url")


@router.post("/{config_id}/test")
async def test_model_config(
    config_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    settings = get_settings()

    if config_id == SYSTEM_DEFAULT_CONFIG_ID:
        if not settings.system_default_model_id:
            raise HTTPException(status_code=404, detail="No system default configured")
        api_key = settings.system_default_api_key
        base_url = settings.system_default_base_url
        model_id = settings.system_default_model_id
        provider = "openai"
        url_mode = "base_url"
    else:
        result = await db.execute(
            select(UserModelConfig).where(
                UserModelConfig.config_id == config_id,
                UserModelConfig.user_id == current_user.user_id,
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Model config not found")

        if not row.encrypted_key:
            return {"ok": False, "error": "No API key configured for this model"}
        if row.provider == "azure_openai" and not row.base_url:
            return {"ok": False, "error": "Azure OpenAI requires a base URL"}

        api_key = decrypt_or_none(row.encrypted_key, row.user_id)
        if api_key is None:
            return {"ok": False, "error": "Stored API key is corrupted — delete and re-save the model config"}
        base_url = row.base_url
        model_id = row.model_id
        provider = row.provider
        url_mode = row.url_mode

    return await _do_test(provider, model_id, api_key, base_url, url_mode)


async def _do_test(provider: str, model_id: str, api_key: str, base_url: str | None, url_mode: str = "base_url") -> dict:
    settings = get_settings()
    try:
        import httpx
        ssl_verify = settings.llm_ssl_verify
        request_params: dict | None = None

        if provider == "anthropic":
            url_base = (base_url or "https://api.anthropic.com").rstrip("/")
            # Exact hostname match — a substring match would send the plaintext
            # key to a lookalike domain (api.anthropic.com.evil.com).
            is_direct = urlsplit(url_base).hostname == "api.anthropic.com"
            if url_mode == "full_url":
                url = url_base
                headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
                payload = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            elif is_direct:
                url = f"{url_base}/v1/messages"
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
                payload = {"model": model_id, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            else:
                url = f"{url_base}/model/{model_id}/invoke"
                headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
                payload = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
        elif provider == "azure_openai":
            url_base = (base_url or "").rstrip("/")
            if not url_base:
                return {"ok": False, "error": "Azure OpenAI requires a valid endpoint"}
            api_version = "2024-08-01-preview"
            url = f"{url_base}/openai/deployments/{model_id}/chat/completions?api-version={api_version}"
            headers = {"api-key": api_key, "content-type": "application/json"}
            payload = {"max_completion_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        elif provider == "google_gemini":
            url_base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
            url = f"{url_base}/v1beta/models/{model_id}:generateContent"
            # Key in the header, never the URL: query params leak into logs.
            headers = {"x-goog-api-key": api_key, "content-type": "application/json"}
            payload = {"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 5}}
        else:
            url_base = (base_url or "https://api.openai.com").rstrip("/")
            if url_mode == "full_url":
                if not url_base.endswith("/chat/completions"):
                    url_base = f"{url_base}/chat/completions"
                url = url_base
            else:
                if not re.search(r"/v\d+$", url_base):
                    url_base = f"{url_base}/v1"
                url = f"{url_base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
            payload = {"model": model_id, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}

        # User-controlled base_url must pass the same SSRF gate as every
        # other outbound URL: literal-IP/metadata checks always on, port
        # allowlist NOT applied (self-hosted gateways on arbitrary ports are
        # legitimate targets), DNS-resolved checks follow the SSRF_ENABLED
        # opt-in convention. resolve_and_validate directly — validate_outbound_url
        # would re-apply the port allowlist .
        from agent_core.tools._ssrf import SSRFError, validate_url as _ssrf_validate_url
        _ssrf_validate_url(url, allow_any_port=True)
        from agent_core.tools._http import _is_ssrf_enabled
        if _is_ssrf_enabled():
            from agent_core.tools._ssrf import resolve_and_validate
            from urllib.parse import urlparse
            _p = urlparse(url)
            resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))

        async with httpx.AsyncClient(timeout=30.0, verify=ssl_verify) as http:
            resp = await http.post(
                url,
                headers=headers,
                json=payload,
                params=request_params,
            )

        if resp.status_code == 200:
            return {"ok": True}
        try:
            body = resp.json()
        except Exception:
            body = {}
        error_msg = body.get("error", {}).get("message", "") or resp.text[:200]
        return {"ok": False, "error": f"HTTP {resp.status_code}: {error_msg}"}
    except SSRFError as e:
        return {"ok": False, "error": f"Blocked URL: {e}"}
    except Exception:
        _log.warning("Model config connection test failed", exc_info=True)
        return {"ok": False, "error": "Connection test failed"}
