from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from agent_framework.core.types import Capability, GenerationRequest, GenerationResponse


class ProviderConfig(BaseModel):
    provider: str
    model: str
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    base_url: str | None = None
    timeout_seconds: float = 30.0
    extra: dict[str, str] = Field(default_factory=dict)

    def cache_key(self) -> str:
        extra_items = tuple(sorted(self.extra.items()))
        return "|".join(
            [
                self.provider,
                self.model,
                self.base_url or "",
                self.api_key or "",
                f"{self.timeout_seconds}",
                repr(extra_items),
            ]
        )


class ModelProviderError(RuntimeError):
    def __init__(self, provider: str, detail: str, status_code: int | None = None) -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        suffix = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"Provider '{provider}' failed{suffix}: {detail}")


class ModelAdapter(ABC):
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def capabilities(self) -> set[Capability]:
        raise NotImplementedError

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        raise NotImplementedError

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        raise NotImplementedError("Streaming not implemented for this provider")

    async def aclose(self) -> None:
        return None
