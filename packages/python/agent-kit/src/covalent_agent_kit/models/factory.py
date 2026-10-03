from __future__ import annotations

from covalent_runtime.ports.model import ModelAdapter, ProviderConfig
from covalent_agent_kit.models.openai_compatible import OpenAICompatibleProvider
from covalent_agent_kit.models.apih import APIHProvider


def build_provider(config: ProviderConfig) -> ModelAdapter:
    if config.provider == "apih":
        if config.api_style == "responses":
            from covalent_agent_kit.models.apih import APIHResponsesProvider

            return APIHResponsesProvider(config)
        return APIHProvider(config)
    if config.provider == "openai_compatible":
        if config.api_style == "responses":
            from covalent_agent_kit.models.openai_responses import ResponsesProvider

            return ResponsesProvider(config)
        return OpenAICompatibleProvider(config)
    if config.provider == "anthropic_compatible":
        from covalent_agent_kit.models.anthropic_compatible import AnthropicCompatibleProvider

        return AnthropicCompatibleProvider(config)
    raise ValueError(f"Unsupported provider: {config.provider}")
