"""Desktop-owned MCP service definition; credentials stay in the host."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from covalent_contracts.mcp import McpServerConfig


class DesktopMcpService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def check_connection(self) -> "DesktopMcpService":
        self.name = self.name.strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", self.name):
            raise ValueError(
                "MCP name must start with a letter and contain only letters, numbers, _ or -"
            )
        if self.transport == "stdio":
            self.command = (self.command or "").strip()
            if not self.command:
                raise ValueError("A stdio MCP service requires a command")
            self.url = None
        else:
            self.url = (self.url or "").strip()
            endpoint = urlparse(self.url)
            if not endpoint.hostname or endpoint.scheme not in {"https", "http"}:
                raise ValueError("MCP service requires an HTTP or HTTPS URL")
            self.command = None
            self.args = []
        return self

    def to_contract(self, env: dict[str, str] | None = None) -> McpServerConfig:
        return McpServerConfig(
            name=self.name,
            transport=self.transport,
            command=self.command,
            args=self.args,
            url=self.url,
            env=env or {},
        )
