from __future__ import annotations

from typing import Any, Protocol

from agent_framework.core.types import ToolResult
from agent_framework.mcp.spec import McpServerConfig, McpToolReference


class McpClient(Protocol):
    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        ...

    async def call_tool(self, server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        ...
