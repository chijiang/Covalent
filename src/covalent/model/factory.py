from __future__ import annotations

from covalent.model.base import ModelAdapter, ProviderConfig
from covalent.model.openai_compatible import OpenAICompatibleProvider


def build_provider(config: ProviderConfig) -> ModelAdapter:
    if config.provider == "openai_compatible":
        return OpenAICompatibleProvider(config)
    raise ValueError(f"Unsupported provider: {config.provider}")
