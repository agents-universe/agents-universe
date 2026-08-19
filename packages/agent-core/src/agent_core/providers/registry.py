"""Provider registry — lazy imports so missing optional SDKs don't break startup."""
from __future__ import annotations

from typing import Any

from .base import LLMProvider

# Lazy import callables: only load the SDK when a provider is actually used.
# This lets other packages import agent_core without installing all LLM SDKs.
_PROVIDER_LOADERS: dict[str, Any] = {
    "anthropic": lambda: __import__(
        "agent_core.providers.anthropic_claude", fromlist=["AnthropicClaudeProvider"]
    ).AnthropicClaudeProvider,
    "openai": lambda: __import__(
        "agent_core.providers.openai", fromlist=["OpenAIProvider"]
    ).OpenAIProvider,
    "azure_openai": lambda: __import__(
        "agent_core.providers.openai", fromlist=["AzureOpenAIProvider"]
    ).AzureOpenAIProvider,
    "google_gemini": lambda: __import__(
        "agent_core.providers.google_gemini", fromlist=["GoogleGeminiProvider"]
    ).GoogleGeminiProvider,
}


def get_provider(provider_key: str, credentials: dict[str, Any]) -> LLMProvider:
    """Instantiate a provider from its key and credential dict.

    Credential keys per provider:
      anthropic:     api_key, model, base_url (opt), url_mode (opt: base_url|full_url)
      openai:        api_key, model, base_url (opt), url_mode (opt: base_url|full_url)
      azure_openai:  api_key, endpoint, deployment, api_version (opt), model (opt)
      google_gemini: api_key, model, base_url (opt), url_mode (opt: base_url|full_url)
    All providers additionally accept context_window (opt: per-config override
    for the name-matched default; None = auto-match by model name).
    """
    loader = _PROVIDER_LOADERS.get(provider_key)
    if loader is None:
        raise ValueError(
            f"Unknown LLM provider: {provider_key!r}. Available: {list(_PROVIDER_LOADERS)}"
        )
    try:
        cls = loader()
    except ImportError as e:
        raise ImportError(
            f"Failed to load provider '{provider_key}': {e}. "
            f"Ensure the required SDK is installed."
        ) from e
    return cls(**credentials)


def default_context_window(provider_key: str, model: str) -> int:
    """Name-matched context window (tokens) a provider would use without an override.

    This is the value shown as the prefill/default in Settings → AI Models and
    what the runtime falls back to when a config leaves context_window unset.
    Lazy imports mirror _PROVIDER_LOADERS so merely calling this never pulls
    in an LLM SDK.
    """
    if provider_key == "anthropic":
        from .anthropic_claude import _context_window
    elif provider_key == "google_gemini":
        from .google_gemini import _context_window
    else:
        # openai + azure_openai share the OpenAI name table.
        from .openai import _context_window
    return _context_window(model)
