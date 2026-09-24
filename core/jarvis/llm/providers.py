"""Build Pydantic AI model objects from `models.yaml` entries."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic_ai.models import Model

from jarvis.llm.config import ModelConfig, ProviderConfig, ProviderKind
from jarvis.llm.fake import fake_model

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"


class ProviderUnavailable(RuntimeError):
    pass


def provider_ready(
    provider: ProviderConfig, env: Mapping[str, str], *, allow_fake: bool
) -> str | None:
    """Return None if the provider can be used, otherwise the reason it can't."""
    if provider.kind == ProviderKind.FAKE:
        return None if allow_fake else "the fake model is disabled outside tests"
    if provider.api_key_env and not env.get(provider.api_key_env):
        return f"{provider.api_key_env} is not set"
    return None


def build_model(
    ref: str,
    cfg: ModelConfig,
    provider: ProviderConfig,
    env: Mapping[str, str],
    *,
    allow_fake: bool,
) -> Model:
    reason = provider_ready(provider, env, allow_fake=allow_fake)
    if reason:
        raise ProviderUnavailable(f"{ref}: {reason}")
    key = env.get(provider.api_key_env) if provider.api_key_env else None
    base_url = provider.base_url
    if provider.base_url_env and env.get(provider.base_url_env):
        base_url = env[provider.base_url_env]

    match provider.kind:
        case ProviderKind.FAKE:
            return fake_model(cfg.model)
        case ProviderKind.OLLAMA:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.ollama import OllamaProvider

            return OpenAIChatModel(
                cfg.model, provider=OllamaProvider(base_url=base_url or DEFAULT_OLLAMA_URL)
            )
        case ProviderKind.GROQ:
            from pydantic_ai.models.groq import GroqModel
            from pydantic_ai.providers.groq import GroqProvider

            return GroqModel(cfg.model, provider=GroqProvider(api_key=key, base_url=base_url))
        case ProviderKind.GOOGLE:
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            return GoogleModel(cfg.model, provider=GoogleProvider(api_key=key))
        case ProviderKind.OPENROUTER:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openrouter import OpenRouterProvider

            return OpenAIChatModel(
                cfg.model, provider=OpenRouterProvider(api_key=key, app_title="Jarvis")
            )
        case ProviderKind.ANTHROPIC:
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            return AnthropicModel(cfg.model, provider=AnthropicProvider(api_key=key))
        case ProviderKind.OPENAI | ProviderKind.OPENAI_COMPATIBLE:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            return OpenAIChatModel(
                cfg.model, provider=OpenAIProvider(base_url=base_url, api_key=key or "unused")
            )
    raise ProviderUnavailable(f"{ref}: unsupported provider kind {provider.kind}")
