from __future__ import annotations
from typing import Literal
from hashlib import sha256
from pydantic import BaseModel, Field
from covalent_contracts.apih import APIHConfig

class ProviderConfig(BaseModel):
    provider: str
    model: str
    api_key: str | None = Field(default=None, exclude=True, repr=False)
    base_url: str | None = None
    # Wire protocol: None/"chat_completions" calls POST {base_url}/chat/completions
    # (Chat Completions API); "responses" calls POST {base_url}/responses (Responses
    # API); "messages" calls POST {base_url}/v1/messages via the Anthropic protocol
    # (anthropic_compatible providers only).
    api_style: Literal["chat_completions", "responses", "messages"] | None = None
    timeout_seconds: float = 500.0
    extra: dict[str, str] = Field(default_factory=dict)
    # Runtime-only: credentials are resolved from the providers store, not agents.
    apih: APIHConfig | None = Field(default=None, exclude=True, repr=False)

    def cache_key(self) -> str:
        extra_items = tuple(sorted(self.extra.items()))
        return "|".join(
            [
                self.provider,
                self.model,
                self.base_url or "",
                self.api_key or "",
                self.api_style or "",
                f"{self.timeout_seconds}",
                repr(extra_items),
                sha256(self.apih.model_dump_json().encode()).hexdigest() if self.apih else "",
            ]
        )


