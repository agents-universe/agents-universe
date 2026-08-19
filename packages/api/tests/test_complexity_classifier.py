"""Tests for api/services/complexity.py — pre-classification for auto routing."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.providers.base import CompletionResult, Message

from api.services import complexity


class FakeProvider:
    """Minimal LLMProvider whose complete() returns a scripted reply."""

    def __init__(self, reply: str | None = None, error: Exception | None = None, delay: float = 0.0):
        self.reply = reply
        self.error = error
        self.delay = delay
        self.closed = False
        self.received_messages: list[Message] = []

    async def complete(self, messages, tools=None, max_tokens=4096, temperature=0.0):
        self.received_messages = list(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return CompletionResult(
            message=Message(role="assistant", content=self.reply or ""),
            usage={},
            model="fake",
            finish_reason="stop",
        )

    async def close(self):
        self.closed = True


def _make_db(*rows) -> MagicMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    db.execute.return_value = result
    return db


def _row(role: str, content: str, seq: int) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, sequence_num=seq)


def _env(provider: FakeProvider | None = None) -> dict:
    return {
        "credentials": {"cfg": {"api_key": "secret"}},
        "tier_models": {"cfg": {"provider": "openai", "model": "cheap-model"}},
        "classifier_config_id": "cfg",
    }


@pytest.fixture
def fake_registry(monkeypatch):
    """Registry returns the scripted provider; captures instantiation calls."""
    captured: dict = {"provider": None}

    def _install(provider: FakeProvider):
        captured["provider"] = provider

        def _get(provider_key: str, creds: dict):
            captured["creds"] = creds
            return provider

        monkeypatch.setattr("agent_core.providers.registry.get_provider", _get)
        return captured

    return _install


# ── parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reply,expected", [("low", "low"), (" mid ", "mid"), ("High\n", "high")])
async def test_parses_tier_reply(fake_registry, reply, expected):
    provider = FakeProvider(reply=reply)
    fake_registry(provider)
    result = await complexity.classify_complexity(
        _make_db(), "conv-1", "hello", **_env(provider)
    )
    assert result == expected


async def test_unparseable_reply_returns_none(fake_registry):
    provider = FakeProvider(reply="I think this is a complex task, maybe.")
    fake_registry(provider)
    result = await complexity.classify_complexity(
        _make_db(), "conv-1", "hello", **_env(provider)
    )
    assert result is None


async def test_provider_error_returns_none(fake_registry):
    provider = FakeProvider(error=RuntimeError("key revoked"))
    fake_registry(provider)
    result = await complexity.classify_complexity(
        _make_db(), "conv-1", "hello", **_env(provider)
    )
    assert result is None


async def test_timeout_returns_none(fake_registry, monkeypatch):
    monkeypatch.setattr(complexity, "_CLASSIFY_TIMEOUT", 0.05)
    provider = FakeProvider(reply="high", delay=0.5)
    fake_registry(provider)
    result = await complexity.classify_complexity(
        _make_db(), "conv-1", "hello", **_env(provider)
    )
    assert result is None


async def test_unknown_config_returns_none(monkeypatch):
    """config not in tier_models → no provider instantiation, no call."""
    calls = []
    monkeypatch.setattr(
        "agent_core.providers.registry.get_provider",
        lambda k, c: calls.append(k) or FakeProvider(reply="low"),
    )
    result = await complexity.classify_complexity(
        _make_db(),
        "conv-1",
        "hello",
        credentials={"cfg": {"api_key": "secret"}},
        tier_models={"cfg": {"provider": "openai", "model": "cheap-model"}},
        classifier_config_id="missing",
    )
    assert result is None
    assert calls == []


# ── history & lifecycle ──────────────────────────────────────────────────


async def test_history_included_and_current_message_last(fake_registry):
    provider = FakeProvider(reply="low")
    fake_registry(provider)
    # Mock returns newest-first (query DESC) — the service reverses to
    # chronological order.
    db = _make_db(
        _row("tool", "should be filtered out", 3),
        _row("assistant", "earlier answer", 2),
        _row("user", "earlier question", 1),
    )
    result = await complexity.classify_complexity(db, "conv-1", "the new ask", **_env(provider))
    assert result == "low"
    roles = [m.role for m in provider.received_messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert provider.received_messages[-1].content == "the new ask"
    assert "earlier question" in provider.received_messages[1].content


async def test_history_char_budget_trims_oldest(fake_registry):
    provider = FakeProvider(reply="high")
    fake_registry(provider)
    long = "x" * 4500
    db = _make_db(
        _row("user", "recent", 2),
        _row("user", long, 1),
    )
    await complexity.classify_complexity(db, "conv-1", "new", **_env(provider))
    contents = [m.content for m in provider.received_messages]
    # recent (6) + long (4500) exceeds the 4000 budget → oldest dropped. Also
    # check the truncated form is absent, so the pass is the budget, not the
    # per-message [:2000] cap.
    assert "recent" in contents
    assert long not in contents
    assert ("x" * 2000) not in contents


async def test_provider_closed_after_call(fake_registry):
    provider = FakeProvider(reply="low")
    fake_registry(provider)
    await complexity.classify_complexity(_make_db(), "conv-1", "hello", **_env(provider))
    assert provider.closed is True


async def test_provider_closed_after_error(fake_registry):
    provider = FakeProvider(error=RuntimeError("boom"))
    fake_registry(provider)
    await complexity.classify_complexity(_make_db(), "conv-1", "hello", **_env(provider))
    assert provider.closed is True
