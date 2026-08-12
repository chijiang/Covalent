"""mcp route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request

from agent_framework.api._auth_helpers import _resolve_console_principal
from agent_framework.api.schemas import McpInspectRequest
from agent_framework.api.schemas import McpInspectResponse
from agent_framework.api.schemas import McpToolCallRequest
from agent_framework.api.schemas import McpToolCallResponse
from agent_framework.api.schemas import McpToolSummaryResponse
from agent_framework.application.services.management_service import _ensure_console_principal_can_access_mcp_server
from agent_framework.infra.db import DatabaseManager
from agent_framework.mcp.client import McpSdkClient

router = APIRouter()


@router.post("/mcp/inspect")
async def inspect_mcp_server(request: Request, inspect_request: McpInspectRequest) -> McpInspectResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    await _ensure_console_principal_can_access_mcp_server(db_manager, principal, inspect_request.server.name)
    client = McpSdkClient()
    try:
        tools = await client.list_tools(inspect_request.server)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return McpInspectResponse(
        server=inspect_request.server,
        tools=[
            McpToolSummaryResponse(
                name=tool.tool_name,
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in tools
        ],
    )

@router.post("/mcp/call")
async def call_mcp_tool(request: Request, call_request: McpToolCallRequest) -> McpToolCallResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    await _ensure_console_principal_can_access_mcp_server(db_manager, principal, call_request.server.name)
    client = McpSdkClient()
    try:
        result = await client.call_tool(call_request.server, call_request.tool_name, call_request.arguments)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return McpToolCallResponse(name=result.name, content=result.content, is_error=result.is_error)
