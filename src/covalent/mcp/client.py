from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from covalent.core.types import ToolResult
from covalent.infra.settings import AppSettings
from covalent.mcp.adapter import McpClient
from covalent.mcp.spec import McpServerConfig, McpToolReference


class McpSdkClient(McpClient):
    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        try:
            async with self._session(server) as session:
                result = await session.list_tools()
        except ExceptionGroup as eg:
            raise eg.exceptions[0] from None
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
        try:
            async with self._session(server) as session:
                result = await session.call_tool(tool_name, arguments=arguments)
        except ExceptionGroup as eg:
            raise eg.exceptions[0] from None
        return ToolResult(
            name=f"mcp__{server.name}__{tool_name}",
            content=self._extract_result_content(result),
            is_error=bool(getattr(result, "isError", False)),
        )

    @asynccontextmanager
    async def _session(self, server: McpServerConfig):
        try:
            import httpx
            from contextlib import AsyncExitStack
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.sse import sse_client
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the 'mcp' package to be installed") from exc

        # The SDK ClientSession default (read_timeout_seconds=None) waits
        # forever for a JSONRPC response; bound it to the configured timeout.
        mcp_read_timeout = timedelta(seconds=AppSettings().mcp_timeout_seconds)

        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"MCP stdio server '{server.name}' is missing a command")
            params = StdioServerParameters(command=server.command, args=server.args, env=server.env or None)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=mcp_read_timeout) as session:
                    await session.initialize()
                    yield session
            return

        if not server.url:
            raise ValueError(f"MCP server '{server.name}' is missing a URL")

        # HTTP transports have no process environment; the configured env
        # key/value pairs are sent as request headers instead.
        headers = dict(server.env) if server.env else None

        if server.transport == "streamable_http":
            # mcp SDK 1.x yields (read, write, get_session_id); 2.0.0 yields
            # (read, write). Unpack shape-agnostically — a fixed 3-tuple unpack
            # raises ValueError inside the SDK's TaskGroup, which the API layer
            # then reports as an opaque "unhandled errors in a TaskGroup".
            async with AsyncExitStack() as stack:
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=headers,
                        # httpx's silent 5s default read timeout aborts slow MCP
                        # data queries before they finish.
                        timeout=httpx.Timeout(AppSettings().mcp_timeout_seconds, connect=10.0),
                    )
                )
                streams = await stack.enter_async_context(
                    streamable_http_client(server.url, http_client=http_client)
                )
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=mcp_read_timeout) as session:
                    await session.initialize()
                    yield session
            return

        if server.transport == "sse":
            def client_factory(headers: dict[str, str] | None = None, timeout: Any = None, auth: Any = None) -> httpx.AsyncClient:
                # sse_client passes Timeout(5, read=300); keep its connect side
                # but raise the read side to the configured MCP timeout so slow
                # data queries can return.
                connect = getattr(timeout, "connect", 5.0) or 5.0
                return httpx.AsyncClient(
                    headers=headers, auth=auth,
                    timeout=httpx.Timeout(connect, read=AppSettings().mcp_timeout_seconds),
                )
            async with sse_client(server.url, headers=headers, httpx_client_factory=client_factory) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=mcp_read_timeout) as session:
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
