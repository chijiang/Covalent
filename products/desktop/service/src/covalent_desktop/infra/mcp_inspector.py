"""MCP inspection adapter for Desktop management."""

from covalent_agent_kit.mcp.client import McpSdkClient
from covalent_contracts.mcp import McpServerConfig


class DesktopMcpInspector:
    async def list_tools(self, server: McpServerConfig) -> list[dict[str, object]]:
        return [
            tool.model_dump(mode="json")
            for tool in await McpSdkClient().list_tools(server)
        ]
