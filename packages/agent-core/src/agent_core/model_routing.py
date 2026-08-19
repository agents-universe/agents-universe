"""Model routing helpers for auto model selection.

Pure functions shared by the API handler (per-turn pre-classification
routing) and agent-core (plan subtask routing). Tiers are low/mid/high;
a missing tier falls back to the nearest available one, cheaper first.
"""
from __future__ import annotations

TIERS = ("low", "mid", "high")

# Nearest-tier fallback chains: a missing tier walks to a cheaper one
# first, then to a more expensive one.
_FALLBACK = {
    "low": ("low", "mid", "high"),
    "mid": ("mid", "low", "high"),
    "high": ("high", "mid", "low"),
}
# Unknown/missing complexity: prefer mid, then cheaper, then pricier.
_DEFAULT_CHAIN = ("mid", "low", "high")


def resolve_tier_config(tier_map: dict[str, str], complexity: str | None) -> str | None:
    """Return the config_id serving the given complexity tier.

    Exact tier match wins; otherwise walk the nearest-tier fallback chain
    (cheaper first). ``complexity`` outside low/mid/high (incl. None) resolves
    via the default chain. Empty ``tier_map`` returns None.
    """
    if not tier_map:
        return None
    chain = _FALLBACK.get(complexity or "", _DEFAULT_CHAIN)
    for tier in chain:
        config_id = tier_map.get(tier)
        if config_id:
            return config_id
    return None


def cheapest_tier(tier_map: dict[str, str]) -> str | None:
    """Return the name of the cheapest configured tier (low→mid→high).

    Used to pick the pre-classifier model in auto mode. None if the map is
    empty.
    """
    for tier in TIERS:
        if tier in tier_map:
            return tier
    return None
