"""tokens route group."""

from __future__ import annotations

from fastapi import APIRouter, Request

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.application.schemas import (
    AgentRunLogResponse,
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenSummaryResponse,
    ApiTokenUpdateRequest,
    ApiTokenUsageByTokenResponse,
    ApiTokenUsageDailyResponse,
    ApiTokenUsageResponse,
)
from covalent.application.audit import RequestMetadata
from covalent.application.services import token_service
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

router = APIRouter(tags=["API Tokens"])


def _request_metadata(request: Request) -> RequestMetadata:
    forwarded_for = request.headers.get("x-forwarded-for")
    client_host = request.client.host if request.client else None
    return RequestMetadata(
        request_id=request.headers.get("x-request-id") or request.headers.get("x-correlation-id"),
        ip_address=(forwarded_for.split(",", 1)[0].strip() if forwarded_for else client_host),
        user_agent=request.headers.get("user-agent"),
    )


def _summary_dto(summary: token_service.TokenSummary) -> ApiTokenSummaryResponse:
    return ApiTokenSummaryResponse(
        id=summary.id,
        name=summary.name,
        user_id=summary.user_id,
        user_email=summary.user_email,
        workspace_id=summary.workspace_id,
        workspace_name=summary.workspace_name,
        token_prefix=summary.token_prefix,
        scopes=list(summary.scopes),
        policy=dict(summary.policy),
        expires_at=summary.expires_at,
        last_used_at=summary.last_used_at,
        revoked_at=summary.revoked_at,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
    )


def _run_dto(entry: token_service.TokenRunLogEntry) -> AgentRunLogResponse:
    return AgentRunLogResponse(
        id=entry.id,
        user_id=entry.user_id,
        token_id=entry.token_id,
        workspace_id=entry.workspace_id,
        agent_name=entry.agent_name,
        memory_mode=entry.memory_mode,
        session_id=entry.session_id,
        status=entry.status,
        latency_ms=entry.latency_ms,
        provider=entry.provider,
        model=entry.model,
        usage=entry.usage,
        error=entry.error,
        metadata=entry.metadata,
        created_at=entry.created_at,
    )


def _usage_dto(overview: token_service.TokenUsageOverview) -> ApiTokenUsageResponse:
    daily = [
        ApiTokenUsageDailyResponse(
            date=d.date,
            requests=d.requests,
            successful_requests=d.successful_requests,
            failed_requests=d.failed_requests,
            total_tokens=d.total_tokens,
            input_tokens=d.input_tokens,
            output_tokens=d.output_tokens,
            average_latency_ms=d.average_latency_ms,
        )
        for d in overview.daily
    ]
    by_token = [
        ApiTokenUsageByTokenResponse(
            token_id=t.token_id,
            token_name=t.token_name,
            token_prefix=t.token_prefix,
            requests=t.requests,
            successful_requests=t.successful_requests,
            failed_requests=t.failed_requests,
            total_tokens=t.total_tokens,
            average_latency_ms=t.average_latency_ms,
            last_used_at=t.last_used_at,
        )
        for t in overview.by_token
    ]
    return ApiTokenUsageResponse(
        days=overview.days,
        starts_at=overview.starts_at,
        ends_at=overview.ends_at,
        active_tokens=overview.active_tokens,
        total_requests=overview.total_requests,
        successful_requests=overview.successful_requests,
        failed_requests=overview.failed_requests,
        total_tokens=overview.total_tokens,
        input_tokens=overview.input_tokens,
        output_tokens=overview.output_tokens,
        average_latency_ms=overview.average_latency_ms,
        daily=daily,
        by_token=by_token,
    )


@router.get("/api-tokens")
async def list_api_tokens(request: Request) -> list[ApiTokenSummaryResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    summaries = await token_service.list_api_token_summaries(db_manager, principal)
    return [_summary_dto(s) for s in summaries]


@router.post("/api-tokens")
async def create_api_token(request: Request, token_request: ApiTokenCreateRequest) -> ApiTokenCreateResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    cmd = token_service.CreateApiTokenCommand(
        name=token_request.name,
        scopes=list(token_request.scopes or []),
        policy=dict(token_request.policy or {}),
        expires_at=token_request.expires_at,
    )
    created = await token_service.create_api_token(
        db_manager, settings, cmd, principal, request_metadata=_request_metadata(request)
    )
    return ApiTokenCreateResponse(**_summary_dto(created.summary).model_dump(), token=created.token)


@router.get("/api-tokens/usage")
async def get_api_token_usage(request: Request, days: int = 30) -> ApiTokenUsageResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    overview = await token_service.get_api_token_usage(db_manager, principal, days=days)
    return _usage_dto(overview)


@router.patch("/api-tokens/{token_id}")
async def update_api_token(
    request: Request,
    token_id: str,
    token_request: ApiTokenUpdateRequest,
) -> ApiTokenSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    cmd = token_service.UpdateApiTokenCommand(
        name=token_request.name,
        scopes=token_request.scopes,
        policy=token_request.policy,
        expires_at=token_request.expires_at,
        fields_to_update=frozenset(token_request.model_fields_set),
    )
    summary = await token_service.update_api_token(
        db_manager, token_id, cmd, principal, request_metadata=_request_metadata(request)
    )
    return _summary_dto(summary)


@router.delete("/api-tokens/{token_id}")
async def revoke_api_token(request: Request, token_id: str) -> ApiTokenSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    summary = await token_service.revoke_api_token(
        db_manager, token_id, principal, request_metadata=_request_metadata(request)
    )
    return _summary_dto(summary)


@router.get("/api-tokens/{token_id}/runs")
async def list_api_token_runs(request: Request, token_id: str, limit: int = 50) -> list[AgentRunLogResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    entries = await token_service.list_api_token_runs(db_manager, token_id, principal, limit=limit)
    return [_run_dto(e) for e in entries]
