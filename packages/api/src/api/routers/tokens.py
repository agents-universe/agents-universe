"""User token management router."""
from __future__ import annotations

import logging
import traceback
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.database import get_db
from api.dependencies.auth import UserInfo, get_current_user
from api.models.user import UserToken
from api.services.token_vault import decrypt_or_none, encrypt, key_hint

router = APIRouter()


def _normalize_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an HTTP or HTTPS URL with a hostname")
    return normalized.rstrip("/")


class TokenUpsert(BaseModel):
    display_name: str | None = Field(None, max_length=255)
    # None => keep the existing key and update metadata only.
    # ("__keep__" is still accepted as a deprecated alias for older clients.)
    # bounded to the encrypted_value column (String(4000)): AES-GCM ciphertext
    # is plaintext+28 bytes and base64 expands it ~4/3, so 2800 chars store as
    # ~3772 chars — longer values raise DataError on MSSQL (500).
    value: str | None = Field(None, max_length=2800)
    base_url: str | None = Field(None, max_length=500)   # custom endpoint override, e.g. internal proxy
    model_id: str | None = Field(None, max_length=200)   # model ID override, e.g. "claude-opus-4-8"

    @field_validator("base_url")
    @classmethod
    def trim_base_url(cls, value: str | None) -> str | None:
        return _normalize_base_url(value)


@router.get("")
async def list_tokens(
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserToken).where(UserToken.user_id == current_user.user_id)
    )
    tokens = result.scalars().all()
    return [
        {
            "service_key": t.service_key,
            "display_name": t.display_name,
            "key_hint": t.key_hint,
            "base_url": t.base_url,
            "model_id": t.model_id,
        }
        for t in tokens
    ]


@router.put("/{service_key}")
async def upsert_token(
    service_key: str = Path(max_length=100),
    body: TokenUpsert = ...,
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    user_id = current_user.user_id

    result = await db.execute(
        select(UserToken).where(
            UserToken.service_key == service_key,
            UserToken.user_id == user_id,
        ).with_for_update()
    )
    token = result.scalar_one_or_none()

    # value=None (or legacy "__keep__") => update metadata only, don't re-encrypt the key
    keep_existing_key = body.value in (None, "__keep__")
    if keep_existing_key:
        if token is None:
            raise HTTPException(status_code=400, detail="No existing token to keep")
        encrypted = token.encrypted_value
        hint = token.key_hint
    else:
        encrypted = encrypt(body.value, user_id)
        hint = key_hint(body.value)

    if token:
        token.encrypted_value = encrypted
        token.key_hint = hint
        if "base_url" in body.model_fields_set:
            token.base_url = body.base_url
        if "model_id" in body.model_fields_set:
            token.model_id = body.model_id or None
        if body.display_name:
            token.display_name = body.display_name
    else:
        token = UserToken(
            user_id=user_id,
            service_key=service_key,
            encrypted_value=encrypted,
            key_hint=hint,
            base_url=body.base_url or None,
            model_id=body.model_id or None,
            display_name=body.display_name,
        )
        db.add(token)

    try:
        await db.commit()
    except IntegrityError:
        # Concurrent PUTs for a missing row both pass the None branch above
        # (with_for_update() locks only existing rows) — the loser violates
        # uq_user_token_service on commit. Mirrors api_keys/tier_models.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Token already exists (concurrent creation)")
    return {"service_key": service_key, "key_hint": hint}
@router.delete("/{service_key}")
async def delete_token(
    service_key: str = Path(max_length=100),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserToken).where(
            UserToken.service_key == service_key,
            UserToken.user_id == current_user.user_id,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    from sqlalchemy import delete
    await db.execute(
        delete(UserToken).where(
            UserToken.service_key == service_key,
            UserToken.user_id == current_user.user_id,
        )
    )
    await db.commit()
    return {"deleted": service_key}


@router.post("/{service_key}/test")
async def test_token(
    service_key: str = Path(max_length=100),
    db: AsyncSession = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),
):
    result = await db.execute(
        select(UserToken).where(
            UserToken.service_key == service_key,
            UserToken.user_id == current_user.user_id,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not configured")

    plain = decrypt_or_none(token.encrypted_value, current_user.user_id)
    if plain is None:
        raise HTTPException(status_code=400, detail="Stored token is corrupted — delete and re-save it")
    # service_key may or may not contain ":" (e.g. "git", "jira", "jira:email", "kong:dev", "kong:uat")
    provider = service_key.split(":", 1)[0]

    try:
        from agent_core.tools._ssrf import SSRFError
        # SSRF gate for the user-controlled stored base_url: literal-IP/
        # metadata checks always on, port allowlist is NOT applied (it is
        # meant for agent-fetched URLs — self-hosted gateways like Kong 8001
        # or Ollama 11434 are legitimate user-configured targets),
        # DNS-resolved checks per SSRF_ENABLED (same policy as the
        # integrations proxy).
        if token.base_url:
            from agent_core.tools._ssrf import validate_url as _ssrf_validate_url
            _ssrf_validate_url(token.base_url, allow_any_port=True)
            # DNS-level check per SSRF_ENABLED — resolve_and_validate directly
            # (validate_outbound_url would re-apply the port allowlist and
            # defeat allow_any_port; self-hosted gateways like Kong:8001 or
            # Ollama:11434 are legitimate, resolved IPs still get blocked —
            # same policy as api_request.
            from agent_core.tools._http import _is_ssrf_enabled
            if _is_ssrf_enabled():
                from agent_core.tools._ssrf import resolve_and_validate
                from urllib.parse import urlparse
                _p = urlparse(token.base_url)
                resolve_and_validate(_p.hostname, _p.port or (443 if _p.scheme == "https" else 80))

        if provider in ("git", "jira", "confluence"):
            # Live connectivity checks live in the shared service so the
            # project-secret test endpoint reuses identical logic.
            from api.services.token_tests import test_service_token
            return await test_service_token(
                service_key, plain, token.base_url, db, current_user.user_id
            )

        if provider == "anthropic":
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            base = (token.base_url or "https://api.anthropic.com").rstrip("/")
            model_id = token.model_id or "claude-haiku-4-5"
            # Exact hostname match, not substring: a base_url like
            # "https://api.anthropic.com.evil.com" must not be treated as the
            # official API — it would receive the plaintext key as x-api-key.
            is_direct = urlsplit(base).hostname == "api.anthropic.com"
            if is_direct:
                url = f"{base}/v1/messages"
                req_headers = {"x-api-key": plain, "anthropic-version": "2023-06-01", "content-type": "application/json"}
                payload = {"model": model_id, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            else:
                url = f"{base}/model/{model_id}/invoke"
                req_headers = {"Authorization": f"Bearer {plain}", "content-type": "application/json"}
                payload = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            async with httpx.AsyncClient(timeout=30.0, verify=cfg.llm_ssl_verify) as http:
                resp = await http.post(url, headers=req_headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "provider": provider, "model": data.get("model", model_id)}
            try:
                body = resp.json()
            except Exception:
                body = {}
            error_msg = body.get("error", {}).get("message", "") or body.get("message", "") or resp.text[:200]
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}: {error_msg}"}

        elif provider == "openai":
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            base = (token.base_url or "https://api.openai.com").rstrip("/")
            is_direct = urlsplit(base).hostname == "api.openai.com"
            if is_direct:
                headers_oai = {"Authorization": f"Bearer {plain}", "content-type": "application/json"}
            else:
                headers_oai = {"Authorization": f"Bearer {plain}", "content-type": "application/json"}
            model_id = token.model_id or ("gpt-4o" if is_direct else "gpt-5")
            # Gateways that already ship the version prefix (Ollama/LiteLLM
            # often store base_url as ".../v1") must not get a doubled
            # /v1/v1/chat/completions — same guard as model_configs._do_test.
            import re
            url_base = base if re.search(r"/v\d+$", base) else f"{base}/v1"
            async with httpx.AsyncClient(timeout=30.0, verify=cfg.llm_ssl_verify) as http:
                resp = await http.post(
                    f"{url_base}/chat/completions",
                    headers=headers_oai,
                    json={"model": model_id, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "provider": provider, "model": data.get("model", model_id)}
            try:
                body = resp.json()
            except Exception:
                body = {}
            error_msg = body.get("error", {}).get("message", "") or resp.content.decode("utf-8", errors="replace")[:200]
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}: {error_msg}"}

        elif provider == "azure_openai":
            import httpx
            from api.config import get_settings as _cfg
            cfg = _cfg()
            base = (token.base_url or "").rstrip("/")
            if not base:
                return {"ok": False, "provider": provider, "error": "Base URL not configured"}
            model_id = token.model_id or "gpt-4o"
            api_version = "2024-08-01-preview"
            url = f"{base}/openai/deployments/{model_id}/chat/completions?api-version={api_version}"
            headers_az = {"api-key": plain, "content-type": "application/json"}
            async with httpx.AsyncClient(timeout=30.0, verify=cfg.llm_ssl_verify) as http:
                resp = await http.post(
                    url,
                    headers=headers_az,
                    json={"max_completion_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
                )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "provider": provider, "model": data.get("model", model_id)}
            try:
                body = resp.json()
            except Exception:
                body = {}
            error_msg = body.get("error", {}).get("message", "") or resp.content.decode("utf-8", errors="replace")[:200]
            return {"ok": False, "provider": provider, "error": f"HTTP {resp.status_code}: {error_msg}"}

        else:
            # jira:email, kong:*, and other plain values - no live test
            return {"ok": True, "provider": provider, "note": "saved"}

    except SSRFError as e:
        return {"ok": False, "provider": provider, "error": f"Blocked URL: {e}"}
    except Exception as exc:
        logger.error("Token test failed for %s: %s", service_key, traceback.format_exc())
        return {"ok": False, "provider": provider, "error": str(exc) or "Token test failed"}
