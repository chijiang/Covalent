from __future__ import annotations

from agent_framework.infra.settings import AppSettings
from agent_framework.model.base import ModelAdapter, ProviderConfig
from agent_framework.model.openai_compatible import OpenAICompatibleProvider


def build_provider(config: ProviderConfig) -> ModelAdapter:
    if config.provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    raise ValueError(f"Unsupported provider: {config.provider}")


def default_provider_config(settings: AppSettings) -> ProviderConfig:
    return ProviderConfig(
        provider=settings.default_provider,
        model=settings.default_model,
        base_url=settings.default_base_url,
        api_key=settings.default_api_key,
        timeout_seconds=settings.request_timeout_seconds,
    )
