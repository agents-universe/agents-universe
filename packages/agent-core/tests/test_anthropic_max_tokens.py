"""max_tokens is clamped to the model's output ceiling before reaching the
Anthropic Messages API.

Agent configs default max_tokens to 128000 (agent.py AgentConfig), which the
Anthropic API rejects with HTTP 400 on models whose output ceiling is lower
(haiku-class 8k, sonnet-class 64k). The OpenAI provider clamps via
_clamp_max_tokens; the Anthropic provider must do the same in all three paths
(direct complete, direct stream, gateway payload)."""

from agent_core.providers.anthropic_claude import (
    AnthropicClaudeProvider,
    _max_output_tokens,
)


def test_max_output_tokens_per_model():
    # haiku-class caps at 8k, sonnet-class at 64k, opus-class at 128k.
    assert _max_output_tokens("claude-haiku-4-5") == 8_192
    assert _max_output_tokens("claude-sonnet-4-6") == 64_000
    assert _max_output_tokens("claude-sonnet-4-6-20250805") == 64_000
    assert _max_output_tokens("claude-opus-5") == 128_000
    assert _max_output_tokens("claude-opus-4-8") == 128_000
    # Unrecognized/legacy models get the conservative sonnet ceiling.
    assert _max_output_tokens("claude-3-haiku-20240307") == 8_192
    assert _max_output_tokens("some-custom-model") == 64_000


def test_gateway_payload_clamps_max_tokens():
    """The default 128000 must be clamped in the gateway payload, not sent raw."""
    p = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6", base_url="https://gateway.example.com"
    )
    from agent_core.providers.base import Message

    messages = [Message(role="user", content="hi")]
    payload = p._gateway_payload(messages, None, max_tokens=128_000, temperature=0.0)
    assert payload["max_tokens"] == 64_000
    # A below-ceiling value passes through untouched.
    payload2 = p._gateway_payload(messages, None, max_tokens=4_096, temperature=0.0)
    assert payload2["max_tokens"] == 4_096


async def test_gateway_complete_sends_clamped_max_tokens(monkeypatch):
    """The clamped value travels end-to-end through _gateway_complete."""
    sent = {}

    async def fake_post(url, headers=None, json=None):
        sent["json"] = json
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                    "model": "claude-sonnet-4-6",
                }

        return _Resp()

    p = AnthropicClaudeProvider(
        api_key="x", model="claude-sonnet-4-6", base_url="https://gateway.example.com"
    )
    p._http = _FakeHttp(fake_post)
    from agent_core.providers.base import Message

    result = await p._gateway_complete(
        [Message(role="user", content="hi")], None, max_tokens=128_000, temperature=0.0
    )
    assert sent["json"]["max_tokens"] == 64_000
    assert result.message.content == "ok"


class _FakeHttp:
    """Minimal httpx-like surface for provider tests."""

    def __init__(self, post):
        self._post = post

    async def post(self, *args, **kwargs):
        return await self._post(*args, **kwargs)

    async def aclose(self):
        pass


async def test_direct_complete_clamps_before_sdk(monkeypatch):
    """Direct SDK path passes the clamped value to messages.create."""
    import agent_core.providers.anthropic_claude as mod

    captured = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            from agent_core.providers.anthropic_claude import CompletionResult

            return _FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    class _FakeResponse:
        content = []
        stop_reason = "end_turn"
        model = "claude-sonnet-4-6"

        class _Usage:
            input_tokens = 5
            output_tokens = 2

        usage = _Usage()

    monkeypatch.setattr(mod.AnthropicClaudeProvider, "_client", FakeClient(), raising=False)
    p = AnthropicClaudeProvider(api_key="x", model="claude-sonnet-4-6")
    p._is_gateway = False
    p._client = FakeClient()
    from agent_core.providers.base import Message

    result = await p.complete([Message(role="user", content="hi")], max_tokens=128_000)
    assert captured["max_tokens"] == 64_000
    assert result.message.content == ""
