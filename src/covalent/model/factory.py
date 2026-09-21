from __future__ import annotations

from covalent.model.base import ModelAdapter, ProviderConfig
from covalent.model.openai_compatible import OpenAICompatibleProvider
from covalent.model.apih import APIHProvider


def build_provider(config: ProviderConfig) -> ModelAdapter:
    if config.provider == "apih":
        if config.api_style == "responses":
            raise ValueError("apih providers do not support api_style='responses'")
        return APIHProvider(config)
    if config.provider == "openai_compatible":
        if config.api_style == "responses":
            from covalent.model.openai_responses import ResponsesProvider

            return ResponsesProvider(config)
        return OpenAICompatibleProvider(config)
    raise ValueError(f"Unsupported provider: {config.provider}")
