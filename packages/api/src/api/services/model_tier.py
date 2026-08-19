"""Complexity-tier inference for user model configs.

A best-effort default for the auto-route tier assignment: model_id keywords
are matched against the naming conventions of the major providers (as of
2026-08). The result is a starting value the user can override in Settings;
unrecognized models get None and are simply not part of auto routing until
the user assigns a tier.

Keep the keyword tables here and the TS mirror in
packages/web/src/utils/modelTier.ts in sync.
"""
from __future__ import annotations

import re

# Tier choices exposed by the model config CRUD API.
TIER_CHOICES = ("low", "mid", "high")

# Vendor-specific override BEFORE the generic scan: on DeepSeek/Qwen/Doubao,
# "flash" is the budget tier (deepseek-v4-flash, qwen3.6-flash replacing the
# retired turbo); on Gemini "flash" is the balanced tier (handled below).
_FLASH_IS_BUDGET_BRANDS = {"deepseek", "qwen", "doubao"}

# Generic keyword tables — matched token-set intersection, low → high → mid
# priority so compound names resolve to the weakest matching tier
# (gpt-4o-mini → mini beats 4o; gpt-5.6-sol-pro → pro beats nothing else).
_LOW_KEYWORDS = {
    "haiku", "luna", "mini", "nano", "lite", "small", "turbo", "air",
    "1b", "3b", "7b", "8b",
}
_HIGH_KEYWORDS = {
    "fable", "sol", "opus", "pro", "ultra", "max", "premium",
    "reasoner", "thinking", "large",
}
_MID_KEYWORDS = {
    "terra", "flash", "sonnet", "4o", "o1", "o3", "o4", "plus", "medium", "70b",
}
# Conservative default tier for Chinese domestic brands whose version naming
# carries no tier signal (kimi-k3, glm-5.2, ...): treat as balanced; the user
# can reassign in Settings.
_BRAND_DEFAULT_MID = {"deepseek", "qwen", "kimi", "glm", "doubao", "hunyuan", "minimax"}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(model_id: str) -> set[str]:
    return set(_TOKEN_SPLIT.split(model_id.lower()))


def _has_brand(tokens: set[str], brand: str) -> bool:
    """Brand detection tolerates glued version digits (qwen3.6 → "qwen3")."""
    return any(t == brand or t.startswith(brand) for t in tokens)


def infer_complexity_tier(provider: str, model_id: str) -> str | None:
    """Infer a default complexity tier from provider + model_id.

    Returns "low" | "mid" | "high" | None (None = cannot infer, user assigns).
    """
    if provider == "azure_openai":
        # Deployment names carry no tier semantics.
        return None

    tokens = _tokens(model_id)
    if not tokens:
        return None

    if "flash" in tokens and any(_has_brand(tokens, b) for b in _FLASH_IS_BUDGET_BRANDS):
        return "low"

    for keyword in _LOW_KEYWORDS:
        if keyword in tokens:
            return "low"
    for keyword in _HIGH_KEYWORDS:
        if keyword in tokens:
            return "high"
    for keyword in _MID_KEYWORDS:
        if keyword in tokens:
            return "mid"
    if any(_has_brand(tokens, b) for b in _BRAND_DEFAULT_MID):
        return "mid"
    return None
