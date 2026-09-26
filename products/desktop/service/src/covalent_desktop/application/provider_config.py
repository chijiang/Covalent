"""Local model provider configuration without credentials."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DesktopProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provider_type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(max_length=2048)
    api_style: Literal["chat_completions", "responses"] = "chat_completions"
    default_model: str = Field(default="", max_length=255)
    models: list[str] = Field(default_factory=list)
    is_default: bool = False
    legacy_credential: bool = False

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", value):
            raise ValueError(
                "Name must start with a letter and contain only letters, numbers, _ or -"
            )
        return value

    @field_validator("models")
    @classmethod
    def normalize_models(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def check_endpoint(self) -> "DesktopProviderConfig":
        self.base_url = self.base_url.strip().rstrip("/")
        endpoint = urlparse(self.base_url)
        if not endpoint.hostname or not (
            endpoint.scheme == "https"
            or (
                endpoint.scheme == "http"
                and endpoint.hostname in {"localhost", "127.0.0.1"}
            )
        ):
            raise ValueError("Use an HTTPS endpoint or an HTTP loopback endpoint")
        self.default_model = self.default_model.strip()
        if self.default_model and self.default_model not in self.models:
            self.models.insert(0, self.default_model)
        return self
