"""Tests for complexity-tier inference (api/services/model_tier.py).

Pure function tests — no DB / Redis / network. Keep in sync with the TS
mirror tests in packages/web/src/utils/modelTier.test.ts.
"""
from __future__ import annotations

import pytest

from api.services.model_tier import infer_complexity_tier


# ── Anthropic ───────────────────────────────────────────────────────────


def test_anthropic_tiers():
    assert infer_complexity_tier("anthropic", "claude-haiku-4-5") == "low"
    assert infer_complexity_tier("anthropic", "claude-sonnet-5") == "mid"
    assert infer_complexity_tier("anthropic", "claude-opus-5") == "high"
    assert infer_complexity_tier("anthropic", "claude-fable-5") == "high"
    assert infer_complexity_tier("anthropic", "claude-3-5-sonnet") == "mid"


# ── OpenAI ──────────────────────────────────────────────────────────────


def test_openai_tiers():
    assert infer_complexity_tier("openai", "gpt-5.6-luna") == "low"
    assert infer_complexity_tier("openai", "gpt-5.6-terra") == "mid"
    assert infer_complexity_tier("openai", "gpt-5.6-sol") == "high"
    assert infer_complexity_tier("openai", "gpt-5.6-sol-pro") == "high"
    assert infer_complexity_tier("openai", "gpt-4o") == "mid"
    assert infer_complexity_tier("openai", "gpt-4o-mini") == "low"
    assert infer_complexity_tier("openai", "gpt-5.4-nano") == "low"
    assert infer_complexity_tier("openai", "o3-mini") == "low"


def test_openai_priority_compound_names():
    """Weakest matching keyword wins: mini beats 4o, pro beats 4o."""
    assert infer_complexity_tier("openai", "gpt-4o-mini") == "low"
    assert infer_complexity_tier("openai", "gpt-4o-pro") == "high"


# ── Gemini ──────────────────────────────────────────────────────────────


def test_gemini_tiers():
    assert infer_complexity_tier("google_gemini", "gemini-3.1-pro") == "high"
    # Gemini's flash is the balanced tier (unlike DeepSeek/Qwen/Doubao).
    assert infer_complexity_tier("google_gemini", "gemini-3.6-flash") == "mid"
    assert infer_complexity_tier("google_gemini", "gemini-3.5-flash-lite") == "low"


# ── DeepSeek / Qwen (flash = budget tier) ───────────────────────────────


def test_deepseek_flash_is_budget():
    assert infer_complexity_tier("openai", "deepseek-v4-pro") == "high"
    assert infer_complexity_tier("openai", "deepseek-v4-flash") == "low"
    # Legacy aliases: reasoner → high, plain chat → brand default mid.
    assert infer_complexity_tier("openai", "deepseek-reasoner") == "high"
    assert infer_complexity_tier("openai", "deepseek-chat") == "mid"


def test_qwen_flash_is_budget():
    assert infer_complexity_tier("openai", "qwen3.7-max") == "high"
    assert infer_complexity_tier("openai", "qwen3.7-plus") == "mid"
    assert infer_complexity_tier("openai", "qwen3.6-flash") == "low"
    assert infer_complexity_tier("openai", "qwen-turbo") == "low"
    assert infer_complexity_tier("openai", "qwen-3.5-omni") == "mid"


# ── Chinese domestic brands (conservative mid default) ──────────────────


def test_domestic_brand_defaults():
    assert infer_complexity_tier("openai", "kimi-k2-thinking") == "high"
    assert infer_complexity_tier("openai", "kimi-k3") == "mid"
    assert infer_complexity_tier("openai", "glm-5.2") == "mid"
    assert infer_complexity_tier("openai", "glm-4-air") == "low"
    assert infer_complexity_tier("openai", "doubao-seed-2.1-pro") == "high"
    assert infer_complexity_tier("openai", "doubao-seed-2.1-turbo") == "low"
    assert infer_complexity_tier("openai", "hunyuan-hy3") == "mid"
    assert infer_complexity_tier("openai", "minimax-m1") == "mid"


# ── Azure / unknown / case ──────────────────────────────────────────────


def test_azure_never_infers():
    assert infer_complexity_tier("azure_openai", "gpt-4o-mini-deployment") is None
    assert infer_complexity_tier("azure_openai", "claude-sonnet") is None


def test_case_insensitive():
    assert infer_complexity_tier("openai", "GPT-4O-MINI") == "low"
    assert infer_complexity_tier("anthropic", "Claude-Opus-5") == "high"


def test_llama_70b_is_mid():
    assert infer_complexity_tier("openai", "llama-3.3-70b") == "mid"


@pytest.mark.parametrize(
    "model_id",
    ["my-custom-model", "gpt-5.6"],
)
def test_unrecognized_returns_none(model_id):
    assert infer_complexity_tier("openai", model_id) is None
