from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_framework.core.types import Capability
from agent_framework.mcp.spec import McpServerConfig, McpToolReference
from agent_framework.model.base import ProviderConfig


class AgentSpec(BaseModel):
    name: str
    description: str
    system_prompt: str
    reasoning_prompt: str = ""
    reasoning_level: str = "none"
    provider: ProviderConfig
    skills: list[str] = Field(default_factory=list)
    local_tools: list[str] = Field(default_factory=list)
    delegate_agents: list[str] = Field(default_factory=list)
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    mcp_tools: list[McpToolReference] = Field(default_factory=list)
    capabilities: set[Capability] = Field(default_factory=lambda: {Capability.CHAT, Capability.REACT})
    max_iterations: int = 6
    metadata: dict[str, Any] = Field(default_factory=dict)
