from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from agent_framework.core.types import ToolResult
from agent_framework.mcp.adapter import McpClient
from agent_framework.mcp.spec import McpServerConfig, McpToolReference


class McpSdkClient(McpClient):
    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        async with self._session(server) as session:
            result = await session.list_tools()
            return [
                McpToolReference(
                    server_name=server.name,
                    tool_name=tool.name,
                    description=getattr(tool, "description", None),
                    input_schema=getattr(tool, "inputSchema", None) or {},
                )
                for tool in result.tools
            ]

    async def call_tool(self, server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        async with self._session(server) as session:
            result = await session.call_tool(tool_name, arguments=arguments)
            return ToolResult(
                name=f"mcp__{server.name}__{tool_name}",
                content=self._extract_result_content(result),
                is_error=bool(getattr(result, "isError", False)),
            )

    @asynccontextmanager
    async def _session(self, server: McpServerConfig):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the 'mcp' package to be installed") from exc

        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"MCP stdio server '{server.name}' is missing a command")
            params = StdioServerParameters(command=server.command, args=server.args, env=server.env or None)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        if not server.url:
            raise ValueError(f"MCP server '{server.name}' is missing a URL")

        if server.transport == "streamable_http":
            async with streamable_http_client(server.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        if server.transport == "sse":
            async with sse_client(server.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        raise ValueError(f"Unsupported MCP transport: {server.transport}")

    @staticmethod
    def _extract_result_content(result: Any) -> Any:
        if getattr(result, "structuredContent", None) is not None:
            return result.structuredContent

        contents = getattr(result, "content", None) or []
        if not contents:
            return ""
        if len(contents) == 1:
            block = contents[0]
            text = getattr(block, "text", None)
            return text if text is not None else str(block)
        extracted: list[Any] = []
        for block in contents:
            text = getattr(block, "text", None)
            extracted.append(text if text is not None else str(block))
        return extracted
