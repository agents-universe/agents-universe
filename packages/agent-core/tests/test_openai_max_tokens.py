"""OpenAI-provider max_tokens clamp: gpt-4o ceiling vs models that take more.

The clamp exists because gpt-4o-class models reject raw 128000 with a 400 —
but applying it to GLM (the system default, thinking always on and not
request-side-disableable) truncated agentic turns at 16384 thinking tokens
with zero content: `stop_reason=max_tokens`, run "Response truncated before
any output". The endpoint accepts 128000 (verified live), so GLM is exempt.
"""
from __future__ import annotations

from agent_core.providers.openai import OpenAIProvider


def _provider(model: str) -> OpenAIProvider:
    p = OpenAIProvider.__new__(OpenAIProvider)  # skip __init__ (no client)
    p._model = model
    return p


def test_glm_exempt_from_output_clamp():
    assert _provider("glm-5.3")._clamp_max_tokens(128000) == 128000
    assert _provider("GLM-4.7")._clamp_max_tokens(65536) == 65536


def test_gpt4o_class_still_clamped():
    p = _provider("gpt-4o")
    assert p._clamp_max_tokens(128000) == 16384


def test_reasoning_models_still_untouched():
    p = _provider("o3-mini")
    assert p._clamp_max_tokens(128000) == 128000


def test_glm_prefix_match_is_model_name_scoped():
    # A model merely *containing* "glm" must not dodge the clamp.
    p = _provider("my-glmish-gateway")
    assert p._clamp_max_tokens(128000) == 16384
