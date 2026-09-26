"""Desktop's persisted Agent configuration and Runtime conversion."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from covalent_contracts.agent import AgentSpec
from covalent_contracts.mcp import McpServerConfig, McpToolReference
from covalent_contracts.messages import Capability
from covalent_contracts.model import ProviderConfig
from covalent_desktop.application.provider_config import DesktopProviderConfig

DESKTOP_CAPABILITIES = {
    Capability.CHAT,
    Capability.REACT,
    Capability.TOOL_CALLING,
    Capability.MCP,
}


class DesktopAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(
        default="You are a helpful assistant.", max_length=20_000
    )
    reasoning_prompt: str = Field(default="", max_length=20_000)
    reasoning_level: Literal["none", "low", "medium", "high", "max"] = "none"
    explicit_thinking: bool = True
    enabled: bool = True
    provider_name: str
    model: str = Field(max_length=255)
    timeout_seconds: int = Field(default=500, ge=1, le=3600)
    max_iterations: int = Field(default=6, ge=1, le=50)
    context_window: int | None = Field(default=None, ge=1024, le=2_000_000)
    skills: list[str] = Field(default_factory=list)
    local_tools: list[str] = Field(default_factory=list)
    allowed_outbound: list[str] = Field(default_factory=list)
    sandbox_profile_id: str | None = None
    delegate_agents: list[str] = Field(default_factory=list)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    mcp_tools: list[McpToolReference] = Field(default_factory=list)
    capabilities: list[Capability] = Field(
        default_factory=lambda: [Capability.CHAT, Capability.REACT]
    )

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", value):
            raise ValueError(
                "Name must start with a letter and contain only letters, numbers, _ or -"
            )
        return value

    @field_validator("model", "provider_name")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required")
        return value

    @field_validator("skills", "local_tools", "delegate_agents", "allowed_outbound")
    @classmethod
    def normalize_names(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: list[Capability]) -> list[Capability]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def check_routing(self) -> "DesktopAgentConfig":
        if self.name in self.delegate_agents:
            raise ValueError("An agent cannot delegate to itself")
        if not set(self.capabilities).issubset(DESKTOP_CAPABILITIES):
            raise ValueError("This capability is not available in Desktop yet")
        if self.sandbox_profile_id is not None:
            raise ValueError("Sandbox profiles are not available in Desktop yet")
        if any(server.env for server in self.mcp_servers):
            raise ValueError(
                "MCP credentials must not be stored in Agent configuration"
            )
        if any(server.transport == "stdio" for server in self.mcp_servers):
            raise ValueError(
                "Desktop MCP currently supports SSE and streamable HTTP transports"
            )
        for server in self.mcp_servers:
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", server.name):
                raise ValueError(
                    "MCP server names must start with a letter and contain only letters, numbers, _ or -"
                )
            if server.command or server.args:
                raise ValueError(
                    "Desktop remote MCP servers cannot use commands or arguments"
                )
            address = urlparse(server.url or "")
            if not address.hostname or not (
                address.scheme == "https"
                or (
                    address.scheme == "http"
                    and address.hostname in {"localhost", "127.0.0.1"}
                )
            ):
                raise ValueError(
                    "MCP servers require an HTTPS URL or HTTP loopback URL"
                )
        names = [server.name for server in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        if any(ref.server_name not in names for ref in self.mcp_tools):
            raise ValueError("Each MCP tool must reference a configured MCP server")
        return self

    def to_spec(self, provider: DesktopProviderConfig, api_key: str) -> AgentSpec:
        return AgentSpec(
            name=self.name,
            description=self.description,
            system_prompt=self.system_prompt,
            reasoning_prompt=self.reasoning_prompt,
            reasoning_level=self.reasoning_level,
            provider=ProviderConfig(
                provider="openai_compatible",
                model=self.model,
                base_url=provider.base_url,
                api_style=provider.api_style,
                timeout_seconds=self.timeout_seconds,
                api_key=api_key,
            ),
            skills=self.skills,
            local_tools=self.local_tools,
            allowed_outbound=self.allowed_outbound,
            delegate_agents=self.delegate_agents,
            mcp_servers=self.mcp_servers,
            mcp_tools=self.mcp_tools,
            capabilities=self.capabilities,
            max_iterations=self.max_iterations,
            context_window=self.context_window,
            metadata={"explicit_thinking": self.explicit_thinking},
        )
