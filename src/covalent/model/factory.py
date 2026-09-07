from __future__ import annotations

from covalent.model.base import ModelAdapter, ProviderConfig
from covalent.model.openai_compatible import OpenAICompatibleProvider
from covalent.model.apih import APIHProvider


def build_provider(config: ProviderConfig) -> ModelAdapter:
    if config.provider == "apih":
        return APIHProvider(config)
    if config.provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    raise ValueError(f"Unsupported provider: {config.provider}")
