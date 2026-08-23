"""Per-config context-window override (Settings → AI Models) and the
name-matched default helper."""

from agent_core.providers.registry import default_context_window, get_provider


def test_openai_override_wins_over_name_match():
    from agent_core.providers.openai import OpenAIProvider

    p = OpenAIProvider(api_key="x", model="gpt-4o", context_window=999_999)
    assert p.context_window == 999_999
    # No override → name-matched default (gpt-4o → 128k).
    assert OpenAIProvider(api_key="x", model="gpt-4o").context_window == 128_000


def test_azure_override_wins_over_name_match():
    from agent_core.providers.openai import AzureOpenAIProvider

    p = AzureOpenAIProvider(api_key="x", endpoint="https://example.openai.azure.com", model="gpt-4o", context_window=200_000)
    assert p.context_window == 200_000
    assert AzureOpenAIProvider(api_key="x", endpoint="https://example.openai.azure.com", model="gpt-4o").context_window == 128_000


def test_anthropic_override_wins_over_name_match():
    from agent_core.providers.anthropic_claude import AnthropicClaudeProvider

    # Gateway mode (non-anthropic host) avoids touching the anthropic SDK.
    kwargs = {"api_key": "x", "base_url": "https://gateway.example.com"}
    p = AnthropicClaudeProvider(model="claude-haiku-4-5", context_window=500_000, **kwargs)
    assert p.context_window == 500_000
    # No override → flagship name matches 1M, haiku falls back to 200k.
    assert AnthropicClaudeProvider(model="claude-sonnet-4-6", **kwargs).context_window == 1_000_000
    assert AnthropicClaudeProvider(model="claude-haiku-4-5", **kwargs).context_window == 200_000


def test_gemini_override_wins_over_name_match():
    from agent_core.providers.google_gemini import GoogleGeminiProvider

    p = GoogleGeminiProvider(api_key="x", model="gemini-2.5-flash", context_window=200_000)
    assert p.context_window == 200_000
    assert GoogleGeminiProvider(api_key="x", model="gemini-2.5-flash").context_window == 1_048_576


def test_registry_flows_context_window_through():
    p = get_provider("openai", {"api_key": "x", "model": "gpt-4o", "context_window": 77_000})
    assert p.context_window == 77_000


def test_default_context_window_name_matching():
    # OpenAI table: gpt-5 → 1M, o-series → 200k, unknown → 128k.
    assert default_context_window("openai", "gpt-5.2") == 1_000_000
    assert default_context_window("openai", "o4-mini") == 200_000
    assert default_context_window("openai", "gpt-4o") == 128_000
    # GLM-5.3 (flagship) → 1M; earlier GLM lines → fallback.
    assert default_context_window("openai", "glm-5.3") == 1_000_000
    assert default_context_window("openai", "glm-5.2") == 128_000
    # azure_openai shares the OpenAI table (deployment names → 128k fallback).
    assert default_context_window("azure_openai", "my-deployment") == 128_000
    # Anthropic table: flagship names → 1M, rest → 200k.
    assert default_context_window("anthropic", "claude-opus-5") == 1_000_000
    assert default_context_window("anthropic", "claude-haiku-4-5") == 200_000
    # Gemini table: ultra → 32k, everything else → 1M.
    assert default_context_window("google_gemini", "gemini-ultra") == 32_768
    assert default_context_window("google_gemini", "gemini-2.5-pro") == 1_048_576


def test_every_provider_accepts_the_api_handler_cred_shape():
    """_build_cred in the API handler passes ssl_verify (and for non-Azure
    configs base_url/url_mode) for EVERY provider - a constructor that
    rejects the kwarg TypeErrors the whole turn at provider construction
    (Gemini did, making the provider unusable)."""
    common = {"api_key": "x", "model": "m", "ssl_verify": False, "context_window": 1000}
    for provider_key, extra in [
        ("anthropic", {"base_url": "https://gateway.example.com"}),
        ("openai", {}),
        ("google_gemini", {}),
    ]:
        creds = {**common, **extra}
        p = get_provider(provider_key, creds)
        assert p.context_window == 1000
    # azure takes endpoint instead of base_url.
    p = get_provider("azure_openai", {**common, "endpoint": "https://example.openai.azure.com"})
    assert p.context_window == 1000
