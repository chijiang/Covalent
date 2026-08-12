"""API token use cases.

Business rules for creating / updating / revoking API tokens and reading their
usage. Framework-independent: no FastAPI, no API DTOs — inputs are commands,
outputs are results, errors are ``ApplicationError`` subclasses. The API layer
maps commands from HTTP requests and results back to response DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from covalent.application._utils import _coerce_positive_int, _dedupe_strings, _new_chat_item_id
from covalent.application.audit import RequestMetadata, record_audit
from covalent.application.crypto import generate_api_token, hash_api_token
from covalent.application.errors import ApplicationError, ConflictError, InvalidInputError, NotFoundError
from covalent.application.principal import Principal
from covalent.infra.db import AgentRunLogRow, ApiTokenRow, DatabaseManager, UserRow, WorkspaceRow
from covalent.infra.settings import AppSettings


@dataclass(frozen=True)
class CreateApiTokenCommand:
    name: str
    scopes: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class UpdateApiTokenCommand:
    name: str | None = None
    scopes: list[str] | None = None
    policy: dict[str, Any] | None = None
    expires_at: datetime | None = None
    fields_to_update: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TokenSummary:
    id: str
    name: str
    user_id: str
    user_email: str
    workspace_id: str
    workspace_name: str
    token_prefix: str
    scopes: list[str]
    policy: dict[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TokenCreated:
    summary: TokenSummary
    token: str


@dataclass(frozen=True)
class TokenRunLogEntry:
    id: str
    user_id: str | None
    token_id: str | None
    workspace_id: str | None
    agent_name: str
    memory_mode: str
    session_id: str | None
    status: str
    latency_ms: int | None
    provider: str | None
    model: str | None
    usage: dict[str, Any]
    error: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class TokenUsageDaily:
    date: str
    requests: int
    successful_requests: int
    failed_requests: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int | None


@dataclass(frozen=True)
class TokenUsageByToken:
    token_id: str
    token_name: str
    token_prefix: str
    requests: int
    successful_requests: int
    failed_requests: int
    total_tokens: int
    average_latency_ms: int | None
    last_used_at: datetime | None


@dataclass(frozen=True)
class TokenUsageOverview:
    days: int
    starts_at: datetime
    ends_at: datetime
    active_tokens: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int | None
    daily: list[TokenUsageDaily]
    by_token: list[TokenUsageByToken]


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


def _token_summary(token: ApiTokenRow, user: UserRow, workspace: WorkspaceRow) -> TokenSummary:
    return TokenSummary(
        id=token.id,
        name=token.name,
        user_id=token.user_id,
        user_email=user.email,
        workspace_id=token.workspace_id,
        workspace_name=workspace.name,
        token_prefix=token.token_prefix,
        scopes=list(token.scopes or []),
        policy=dict(token.policy_json or {}),
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
        created_at=token.created_at,
        updated_at=token.updated_at,
    )


async def create_api_token(
    db_manager: DatabaseManager,
    settings: AppSettings,
    cmd: CreateApiTokenCommand,
    principal: Principal,
    request_metadata: RequestMetadata | None = None,
) -> TokenCreated:
    token, token_prefix = generate_api_token()
    token_hash = hash_api_token(token, settings.api_token_hash_pepper)
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, principal.user_id)
            workspace = await session.get(WorkspaceRow, principal.workspace_id)
            if user is None or workspace is None:
                raise NotFoundError("Current token owner or workspace was not found")
            row = ApiTokenRow(
                id=_new_chat_item_id("token"),
                user_id=user.id,
                workspace_id=workspace.id,
                name=cmd.name.strip(),
                token_prefix=token_prefix,
                token_hash=token_hash,
                scopes=_normalize_token_scopes(cmd.scopes),
                policy_json=_normalize_token_policy(cmd.policy),
                expires_at=cmd.expires_at,
            )
            session.add(row)
            await session.flush()
            user = await session.get(UserRow, row.user_id)
            workspace = await session.get(WorkspaceRow, row.workspace_id)
            if user is None or workspace is None:
                raise ApplicationError("API token owner was not saved")
            summary = _token_summary(row, user, workspace)
    await record_audit(
        db_manager,
        action="api_token.created",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request_metadata=request_metadata,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id, "scopes": summary.scopes},
    )
    return TokenCreated(summary=summary, token=token)


async def update_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    cmd: UpdateApiTokenCommand,
    principal: Principal,
    request_metadata: RequestMetadata | None = None,
) -> TokenSummary:
    changed_fields = cmd.fields_to_update
    if not changed_fields:
        raise InvalidInputError("At least one API token field must be provided")

    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None or saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise NotFoundError(f"Unknown API token: {token_id}")
            if saved.revoked_at is not None:
                raise ConflictError("Revoked API tokens cannot be updated")

            if "name" in changed_fields:
                normalized_name = (cmd.name or "").strip()
                if not normalized_name:
                    raise InvalidInputError("API token name must not be empty")
                saved.name = normalized_name
            if "scopes" in changed_fields:
                saved.scopes = _normalize_token_scopes(cmd.scopes or [])
            if "policy" in changed_fields:
                saved.policy_json = _normalize_token_policy(cmd.policy or {})
            if "expires_at" in changed_fields:
                saved.expires_at = cmd.expires_at
            saved.updated_at = datetime.now(UTC)

            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise ApplicationError("API token owner is missing")
            summary = _token_summary(saved, user, workspace)

    await record_audit(
        db_manager,
        action="api_token.updated",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request_metadata=request_metadata,
        metadata={
            "token_prefix": summary.token_prefix,
            "token_user_id": summary.user_id,
            "changed_fields": sorted(changed_fields),
        },
    )
    return summary


async def revoke_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    principal: Principal,
    request_metadata: RequestMetadata | None = None,
) -> TokenSummary:
    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None:
                raise NotFoundError(f"Unknown API token: {token_id}")
            if saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise NotFoundError(f"Unknown API token: {token_id}")
            if saved.revoked_at is None:
                revoked_at = datetime.now(UTC)
                saved.revoked_at = revoked_at
                saved.updated_at = revoked_at
            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise ApplicationError("API token owner is missing")
            summary = _token_summary(saved, user, workspace)
    await record_audit(
        db_manager,
        action="api_token.revoked",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request_metadata=request_metadata,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id},
    )
    return summary


async def list_api_token_summaries(
    db_manager: DatabaseManager,
    principal: Principal,
) -> list[TokenSummary]:
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
        rows = (await session.execute(stmt)).all()
        return [_token_summary(token, user, workspace) for token, user, workspace in rows]


def _token_run_log_entry(row: AgentRunLogRow) -> TokenRunLogEntry:
    return TokenRunLogEntry(
        id=row.id,
        user_id=row.user_id,
        token_id=row.token_id,
        workspace_id=row.workspace_id,
        agent_name=row.agent_name,
        memory_mode=row.memory_mode,
        session_id=row.session_id,
        status=row.status,
        latency_ms=row.latency_ms,
        provider=row.provider,
        model=row.model,
        usage=dict(row.usage_json or {}),
        error=dict(row.error_json or {}),
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
    )


async def list_api_token_runs(
    db_manager: DatabaseManager,
    token_id: str,
    principal: Principal,
    *,
    limit: int = 50,
) -> list[TokenRunLogEntry]:
    bounded_limit = min(max(limit, 1), 200)
    async with db_manager.session_factory() as session:
        token = await session.get(ApiTokenRow, token_id)
        if token is None:
            raise NotFoundError(f"Unknown API token: {token_id}")
        if token.user_id != principal.user_id or token.workspace_id != principal.workspace_id:
            raise NotFoundError(f"Unknown API token: {token_id}")
        rows = (
            await session.execute(
                select(AgentRunLogRow)
                .where(AgentRunLogRow.token_id == token_id)
                .order_by(AgentRunLogRow.created_at.desc())
                .limit(bounded_limit)
            )
        ).scalars()
        return [_token_run_log_entry(row) for row in rows]


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(int(value), 0)
        if isinstance(value, str):
            try:
                return max(int(value.strip()), 0)
            except ValueError:
                continue
    return 0


def build_api_token_usage(
    tokens: list[ApiTokenRow],
    runs: list[AgentRunLogRow],
    *,
    days: int,
    now: datetime,
) -> TokenUsageOverview:
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
        TokenUsageDaily(
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
        TokenUsageByToken(
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

    return TokenUsageOverview(
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


async def get_api_token_usage(
    db_manager: DatabaseManager,
    principal: Principal,
    *,
    days: int = 30,
) -> TokenUsageOverview:
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
    return build_api_token_usage(tokens, runs, days=bounded_days, now=now)
