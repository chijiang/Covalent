from __future__ import annotations

from typing import Any, Protocol

from covalent_runtime.domain.types import ToolResult
from covalent_contracts.mcp import McpServerConfig, McpToolReference


class McpClient(Protocol):
    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        ...

    async def call_tool(self, server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        ...
