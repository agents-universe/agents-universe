"""Anthropic `_thinking_param` matrix: adaptive vs budget shapes, the env
kill-switch, and the budget boundary that a 400 would otherwise enforce."""
from __future__ import annotations

from agent_core.providers.anthropic_claude import AnthropicClaudeProvider
from agent_core.providers.base import thinking_enabled_by_env


def _provider(model: str, enabled: bool = True) -> AnthropicClaudeProvider:
    p = AnthropicClaudeProvider.__new__(AnthropicClaudeProvider)
    p._model = model
    p._thinking_enabled = enabled
    return p


def test_env_kill_switch_returns_none():
    p = _provider("claude-sonnet-4-6", enabled=False)
    assert p._thinking_param(64_000) is None


def test_env_switch_parses_falsy_values(monkeypatch):
    for value in ("0", "false", "no", "False", "NO"):
        monkeypatch.setenv("AGENT_EXTENDED_THINKING", value)
        assert thinking_enabled_by_env() is False, value
    monkeypatch.setenv("AGENT_EXTENDED_THINKING", "1")
    assert thinking_enabled_by_env() is True
    monkeypatch.delenv("AGENT_EXTENDED_THINKING")
    assert thinking_enabled_by_env() is True  # default on


def test_adaptive_families_get_display_summarized():
    """4.6+/5 families must carry display:"summarized" — the default
    "omitted" streams empty thinking deltas and silently breaks the UI.
    They must NEVER see budget_tokens (HTTP 400)."""
    for model in (
        "claude-fable-5",
        "claude-mythos-5-20260301",
        "claude-opus-5",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    ):
        p = _provider(model)
        param = p._thinking_param(64_000)
        assert param == {"type": "adaptive", "display": "summarized"}, model
        assert "budget_tokens" not in param, model


def test_haiku_gets_budget_tokens():
    p = _provider("claude-haiku-4-5")
    param = p._thinking_param(8192)
    assert param == {"type": "enabled", "budget_tokens": 4096}


def test_haiku_budget_boundary():
    # budget = min(4096, max_tokens - 1024); below the API's 1024 floor the
    # param is skipped entirely rather than sent invalid.
    p = _provider("claude-haiku-4-5")
    assert p._thinking_param(2048) == {"type": "enabled", "budget_tokens": 1024}
    assert p._thinking_param(1024) is None
    assert p._thinking_param(512) is None


def test_unknown_claude_defaults_to_adaptive():
    p = _provider("claude-nextgen-9")
    assert p._thinking_param(64_000) == {"type": "adaptive", "display": "summarized"}


def test_disable_thinking_is_sticky():
    p = _provider("claude-sonnet-4-6")
    assert p._thinking_param(64_000) is not None
    p._disable_thinking("gateway rejected the thinking field")
    assert p._thinking_param(64_000) is None
