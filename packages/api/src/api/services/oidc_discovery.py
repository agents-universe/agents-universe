"""OIDC Discovery — fetch and cache provider endpoints from .well-known/openid-configuration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass(frozen=True)
class OIDCEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    revocation_endpoint: Optional[str] = None
    end_session_endpoint: Optional[str] = None


_cache: dict[str, OIDCEndpoints] = {}


async def discover(issuer_url: str) -> OIDCEndpoints:
    """Fetch OIDC discovery document and return parsed endpoints. Results are cached."""
    if issuer_url in _cache:
        return _cache[issuer_url]

    url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    # verify flag was hardcoded False; honor the oauth_ssl_verify
    # setting so self-hosted SSO stays usable while production can enforce
    # real TLS verification.
    from api.config import get_settings
    verify = get_settings().oauth_ssl_verify
    async with httpx.AsyncClient(verify=verify, timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    doc = resp.json()
    endpoints = OIDCEndpoints(
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        userinfo_endpoint=doc["userinfo_endpoint"],
        revocation_endpoint=doc.get("revocation_endpoint"),
        end_session_endpoint=doc.get("end_session_endpoint"),
    )
    _cache[issuer_url] = endpoints
    return endpoints


def clear_cache() -> None:
    _cache.clear()
