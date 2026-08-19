"""Tests for model_routing.resolve_tier_config / cheapest_tier."""
from __future__ import annotations

import pytest

from agent_core.model_routing import TIERS, cheapest_tier, resolve_tier_config


# ── resolve_tier_config ──────────────────────────────────────────────────


def test_exact_match():
    """Exact tier hit wins over every fallback."""
    tier_map = {"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"}
    for tier, cfg in tier_map.items():
        assert resolve_tier_config(tier_map, tier) == cfg


def test_high_missing_falls_back_to_mid_then_low():
    tier_map = {"low": "cfg-low", "mid": "cfg-mid"}
    assert resolve_tier_config(tier_map, "high") == "cfg-mid"
    tier_map = {"low": "cfg-low"}
    assert resolve_tier_config(tier_map, "high") == "cfg-low"


def test_mid_missing_falls_back_to_low_then_high():
    """Cost-first: mid → low before high."""
    tier_map = {"low": "cfg-low", "high": "cfg-high"}
    assert resolve_tier_config(tier_map, "mid") == "cfg-low"
    tier_map = {"high": "cfg-high"}
    assert resolve_tier_config(tier_map, "mid") == "cfg-high"


def test_low_missing_falls_back_to_mid_then_high():
    tier_map = {"mid": "cfg-mid", "high": "cfg-high"}
    assert resolve_tier_config(tier_map, "low") == "cfg-mid"
    tier_map = {"high": "cfg-high"}
    assert resolve_tier_config(tier_map, "low") == "cfg-high"


def test_empty_map_returns_none():
    assert resolve_tier_config({}, "low") is None
    assert resolve_tier_config({}, None) is None


def test_unknown_complexity_uses_default_chain_mid_first():
    tier_map = {"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"}
    assert resolve_tier_config(tier_map, "ultra") == "cfg-mid"
    assert resolve_tier_config(tier_map, None) == "cfg-mid"
    tier_map = {"low": "cfg-low", "high": "cfg-high"}
    assert resolve_tier_config(tier_map, None) == "cfg-low"
    tier_map = {"high": "cfg-high"}
    assert resolve_tier_config(tier_map, None) == "cfg-high"


# ── cheapest_tier ───────────────────────────────────────────────────────


def test_cheapest_prefers_low():
    assert cheapest_tier({"low": "a", "mid": "b", "high": "c"}) == "low"
    assert cheapest_tier({"mid": "b", "high": "c"}) == "mid"
    assert cheapest_tier({"high": "c"}) == "high"


def test_cheapest_empty_map():
    assert cheapest_tier({}) is None


def test_tiers_order_is_low_mid_high():
    assert TIERS == ("low", "mid", "high")


@pytest.mark.parametrize(
    ("complexity", "expected"),
    [("low", "cfg-low"), ("mid", "cfg-mid"), ("high", "cfg-high"), (None, "cfg-mid"), ("bogus", "cfg-mid")],
)
def test_full_map_never_falls_back(complexity, expected):
    """With all tiers present every input resolves to its own/default tier."""
    tier_map = {"low": "cfg-low", "mid": "cfg-mid", "high": "cfg-high"}
    assert resolve_tier_config(tier_map, complexity) == expected
