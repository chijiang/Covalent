from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "sse", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class McpToolReference(BaseModel):
    server_name: str
    tool_name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
