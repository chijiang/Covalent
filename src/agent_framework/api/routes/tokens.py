"""tokens route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import Request

from agent_framework.api._auth_helpers import _get_api_token_usage
from agent_framework.api._auth_helpers import _list_api_token_runs
from agent_framework.api._auth_helpers import _list_api_token_summaries
from agent_framework.api._auth_helpers import _resolve_console_principal
from agent_framework.api.schemas import AgentRunLogResponse
from agent_framework.api.schemas import ApiTokenCreateRequest
from agent_framework.api.schemas import ApiTokenCreateResponse
from agent_framework.api.schemas import ApiTokenSummaryResponse
from agent_framework.api.schemas import ApiTokenUpdateRequest
from agent_framework.api.schemas import ApiTokenUsageResponse
from agent_framework.application.services.token_service import _create_api_token
from agent_framework.application.services.token_service import _revoke_api_token
from agent_framework.application.services.token_service import _update_api_token
from agent_framework.infra.db import DatabaseManager
from agent_framework.infra.settings import AppSettings

router = APIRouter()


@router.get("/api-tokens")
async def list_api_tokens(request: Request) -> list[ApiTokenSummaryResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _list_api_token_summaries(db_manager, principal)

@router.post("/api-tokens")
async def create_api_token(request: Request, token_request: ApiTokenCreateRequest) -> ApiTokenCreateResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _create_api_token(db_manager, settings, token_request, principal, request)

@router.get("/api-tokens/usage")
async def get_api_token_usage(request: Request, days: int = 30) -> ApiTokenUsageResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _get_api_token_usage(db_manager, principal, days=days)

@router.patch("/api-tokens/{token_id}")
async def update_api_token(
    request: Request,
    token_id: str,
    token_request: ApiTokenUpdateRequest,
) -> ApiTokenSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _update_api_token(db_manager, token_id, token_request, principal, request)

@router.delete("/api-tokens/{token_id}")
async def revoke_api_token(request: Request, token_id: str) -> ApiTokenSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _revoke_api_token(db_manager, token_id, principal, request)

@router.get("/api-tokens/{token_id}/runs")
async def list_api_token_runs(request: Request, token_id: str, limit: int = 50) -> list[AgentRunLogResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _list_api_token_runs(db_manager, token_id, principal, limit=limit)
