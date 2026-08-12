"""API token use cases.

Extracted from ``api._auth_helpers`` into the application layer. These are the
business rules for creating / updating / revoking API tokens, independent of
the HTTP layer. Dependencies (``db_manager``, ``settings``) are passed in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select

from covalent.api._shared import (
    ConsolePrincipalContext,
    _api_token_summary_response,
    _coerce_positive_int,
    _agent_run_log_response,
    _dedupe_strings,
    _new_chat_item_id,
    _record_audit_log,
    _usage_int,
)
from covalent.api.auth import generate_api_token, hash_api_token
from covalent.api.schemas import (
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    AgentRunLogResponse,
    ApiTokenSummaryResponse,
    ApiTokenUpdateRequest,
    ApiTokenUsageByTokenResponse,
    ApiTokenUsageDailyResponse,
    ApiTokenUsageResponse,
)
from covalent.infra.db import AgentRunLogRow, ApiTokenRow, DatabaseManager, UserRow, WorkspaceRow
from covalent.infra.settings import AppSettings

def _normalize_token_scopes(scopes: list[str]) -> list[str]:
    normalized = _dedupe_strings([scope.strip() for scope in scopes if isinstance(scope, str)])
    return normalized or ["agent:invoke"]

def _normalize_token_policy(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(policy or {})
    if "allowed_agents" in normalized and isinstance(normalized["allowed_agents"], list):
        normalized["allowed_agents"] = _dedupe_strings([str(value) for value in normalized["allowed_agents"]])
    if "allowed_memory_modes" in normalized and isinstance(normalized["allowed_memory_modes"], list):
        allowed_modes = [str(value) for value in normalized["allowed_memory_modes"] if str(value) in {"none", "session"}]
        normalized["allowed_memory_modes"] = _dedupe_strings(allowed_modes)
    if str(normalized.get("max_trace_level") or "") not in {"none", "steps", "debug"}:
        normalized.pop("max_trace_level", None)
    for key in ("max_requests_per_minute", "max_requests_per_day", "max_tokens_per_day"):
        if key not in normalized:
            continue
        value = _coerce_positive_int(normalized[key])
        if value is None:
            normalized.pop(key, None)
        else:
            normalized[key] = value
    return normalized

async def _create_api_token(
    db_manager: DatabaseManager,
    settings: AppSettings,
    request: ApiTokenCreateRequest,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenCreateResponse:
    token, token_prefix = generate_api_token()
    token_hash = hash_api_token(token, settings.api_token_hash_pepper)
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, principal.user_id)
            workspace = await session.get(WorkspaceRow, principal.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=404, detail="Current token owner or workspace was not found")
            row = ApiTokenRow(
                id=_new_chat_item_id("token"),
                user_id=user.id,
                workspace_id=workspace.id,
                name=request.name.strip(),
                token_prefix=token_prefix,
                token_hash=token_hash,
                scopes=_normalize_token_scopes(request.scopes),
                policy_json=_normalize_token_policy(request.policy),
                expires_at=request.expires_at,
            )
            session.add(row)
            await session.flush()
            user = await session.get(UserRow, row.user_id)
            workspace = await session.get(WorkspaceRow, row.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner was not saved")
            summary = _api_token_summary_response(row, user, workspace)
    await _record_audit_log(
        db_manager,
        action="api_token.created",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id, "scopes": summary.scopes},
    )
    return ApiTokenCreateResponse(**summary.model_dump(), token=token)

async def _update_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    request: ApiTokenUpdateRequest,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenSummaryResponse:
    changed_fields = set(request.model_fields_set)
    if not changed_fields:
        raise HTTPException(status_code=400, detail="At least one API token field must be provided")

    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None or saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.revoked_at is not None:
                raise HTTPException(status_code=409, detail="Revoked API tokens cannot be updated")

            if "name" in changed_fields:
                normalized_name = (request.name or "").strip()
                if not normalized_name:
                    raise HTTPException(status_code=422, detail="API token name must not be empty")
                saved.name = normalized_name
            if "scopes" in changed_fields:
                saved.scopes = _normalize_token_scopes(request.scopes or [])
            if "policy" in changed_fields:
                saved.policy_json = _normalize_token_policy(request.policy or {})
            if "expires_at" in changed_fields:
                saved.expires_at = request.expires_at
            saved.updated_at = datetime.now(UTC)

            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner is missing")
            summary = _api_token_summary_response(saved, user, workspace)

    await _record_audit_log(
        db_manager,
        action="api_token.updated",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={
            "token_prefix": summary.token_prefix,
            "token_user_id": summary.user_id,
            "changed_fields": sorted(changed_fields),
        },
    )
    return summary

async def _revoke_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenSummaryResponse:
    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.revoked_at is None:
                revoked_at = datetime.now(UTC)
                saved.revoked_at = revoked_at
                saved.updated_at = revoked_at
            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner is missing")
            summary = _api_token_summary_response(saved, user, workspace)
    await _record_audit_log(
        db_manager,
        action="api_token.revoked",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id},
    )
    return summary


async def _list_api_token_summaries(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
) -> list[ApiTokenSummaryResponse]:
    async with db_manager.session_factory() as session:
        stmt = (
            select(ApiTokenRow, UserRow, WorkspaceRow)
            .join(UserRow, ApiTokenRow.user_id == UserRow.id)
            .join(WorkspaceRow, ApiTokenRow.workspace_id == WorkspaceRow.id)
            .order_by(ApiTokenRow.created_at.desc())
        )
        stmt = stmt.where(
            ApiTokenRow.user_id == principal.user_id,
            ApiTokenRow.workspace_id == principal.workspace_id,
        )
        rows = (
            await session.execute(stmt)
        ).all()
        return [_api_token_summary_response(token, user, workspace) for token, user, workspace in rows]

async def _list_api_token_runs(
    db_manager: DatabaseManager,
    token_id: str,
    principal: ConsolePrincipalContext,
    *,
    limit: int = 50,
) -> list[AgentRunLogResponse]:
    bounded_limit = min(max(limit, 1), 200)
    async with db_manager.session_factory() as session:
        token = await session.get(ApiTokenRow, token_id)
        if token is None:
            raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
        if token.user_id != principal.user_id or token.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
        rows = (
            await session.execute(
                select(AgentRunLogRow)
                .where(AgentRunLogRow.token_id == token_id)
                .order_by(AgentRunLogRow.created_at.desc())
                .limit(bounded_limit)
            )
        ).scalars()
        return [_agent_run_log_response(row) for row in rows]

def _build_api_token_usage_response(
    tokens: list[ApiTokenRow],
    runs: list[AgentRunLogRow],
    *,
    days: int,
    now: datetime,
) -> ApiTokenUsageResponse:
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    daily_metrics: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        day = (starts_at + timedelta(days=offset)).date().isoformat()
        daily_metrics[day] = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "latencies": [],
        }

    token_metrics: dict[str, dict[str, Any]] = {
        token.id: {
            "token": token,
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "latencies": [],
        }
        for token in tokens
    }

    total_requests = 0
    successful_requests = 0
    failed_requests = 0
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    latencies: list[int] = []

    for run in runs:
        if run.token_id not in token_metrics or run.created_at is None:
            continue
        created_at = run.created_at if run.created_at.tzinfo is not None else run.created_at.replace(tzinfo=UTC)
        if created_at < starts_at or created_at > now:
            continue
        day_key = created_at.date().isoformat()
        day = daily_metrics.get(day_key)
        if day is None:
            continue

        usage = dict(run.usage_json or {})
        run_input_tokens = _usage_int(usage, "input_tokens", "prompt_tokens")
        run_output_tokens = _usage_int(usage, "output_tokens", "completion_tokens")
        run_total_tokens = _usage_int(usage, "total_tokens") or run_input_tokens + run_output_tokens
        succeeded = run.status == "completed"

        total_requests += 1
        successful_requests += int(succeeded)
        failed_requests += int(not succeeded)
        total_tokens += run_total_tokens
        input_tokens += run_input_tokens
        output_tokens += run_output_tokens
        if run.latency_ms is not None:
            latencies.append(run.latency_ms)

        day["requests"] += 1
        day["successful_requests"] += int(succeeded)
        day["failed_requests"] += int(not succeeded)
        day["total_tokens"] += run_total_tokens
        day["input_tokens"] += run_input_tokens
        day["output_tokens"] += run_output_tokens
        if run.latency_ms is not None:
            day["latencies"].append(run.latency_ms)

        token = token_metrics[run.token_id]
        token["requests"] += 1
        token["successful_requests"] += int(succeeded)
        token["failed_requests"] += int(not succeeded)
        token["total_tokens"] += run_total_tokens
        if run.latency_ms is not None:
            token["latencies"].append(run.latency_ms)

    active_tokens = sum(
        1
        for token in tokens
        if token.revoked_at is None
        and (
            token.expires_at is None
            or (token.expires_at if token.expires_at.tzinfo is not None else token.expires_at.replace(tzinfo=UTC)) > now
        )
    )

    daily = [
        ApiTokenUsageDailyResponse(
            date=day,
            requests=metrics["requests"],
            successful_requests=metrics["successful_requests"],
            failed_requests=metrics["failed_requests"],
            total_tokens=metrics["total_tokens"],
            input_tokens=metrics["input_tokens"],
            output_tokens=metrics["output_tokens"],
            average_latency_ms=(
                round(sum(metrics["latencies"]) / len(metrics["latencies"])) if metrics["latencies"] else None
            ),
        )
        for day, metrics in daily_metrics.items()
    ]

    by_token = [
        ApiTokenUsageByTokenResponse(
            token_id=token_id,
            token_name=metrics["token"].name,
            token_prefix=metrics["token"].token_prefix,
            requests=metrics["requests"],
            successful_requests=metrics["successful_requests"],
            failed_requests=metrics["failed_requests"],
            total_tokens=metrics["total_tokens"],
            average_latency_ms=(
                round(sum(metrics["latencies"]) / len(metrics["latencies"])) if metrics["latencies"] else None
            ),
            last_used_at=metrics["token"].last_used_at,
        )
        for token_id, metrics in sorted(
            token_metrics.items(),
            key=lambda item: (-item[1]["requests"], item[1]["token"].name.lower()),
        )
    ]

    return ApiTokenUsageResponse(
        days=days,
        starts_at=starts_at,
        ends_at=now,
        active_tokens=active_tokens,
        total_requests=total_requests,
        successful_requests=successful_requests,
        failed_requests=failed_requests,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        average_latency_ms=round(sum(latencies) / len(latencies)) if latencies else None,
        daily=daily,
        by_token=by_token,
    )

async def _get_api_token_usage(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    *,
    days: int = 30,
) -> ApiTokenUsageResponse:
    bounded_days = min(max(days, 1), 365)
    now = datetime.now(UTC)
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=bounded_days - 1)
    async with db_manager.session_factory() as session:
        tokens = list(
            (
                await session.execute(
                    select(ApiTokenRow)
                    .where(
                        ApiTokenRow.user_id == principal.user_id,
                        ApiTokenRow.workspace_id == principal.workspace_id,
                    )
                    .order_by(ApiTokenRow.created_at.desc())
                )
            ).scalars()
        )
        token_ids = [token.id for token in tokens]
        runs: list[AgentRunLogRow] = []
        if token_ids:
            runs = list(
                (
                    await session.execute(
                        select(AgentRunLogRow)
                        .where(
                            AgentRunLogRow.token_id.in_(token_ids),
                            AgentRunLogRow.created_at >= starts_at,
                            AgentRunLogRow.created_at <= now,
                        )
                        .order_by(AgentRunLogRow.created_at.asc())
                    )
                ).scalars()
            )
    return _build_api_token_usage_response(tokens, runs, days=bounded_days, now=now)
