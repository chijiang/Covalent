"""Audit log use cases.

Extracted from ``api._auth_helpers`` into the application layer.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from covalent.application.errors import ForbiddenError
from covalent.application.principal import Principal
from covalent.application.schemas import QueryStatDay, QueryStatsResponse, UserQueryStat
from covalent.infra.db import AuditLogRow, DatabaseManager, UserRow


@dataclass(frozen=True)
class AuditLogEntry:
    id: str
    action: str
    target_type: str
    target_id: str | None = None
    outcome: str = "success"
    actor_user_id: str | None = None
    actor_token_id: str | None = None
    workspace_id: str | None = None
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


def _audit_log_entry(row: AuditLogRow) -> AuditLogEntry:
    return AuditLogEntry(
        id=row.id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        outcome=row.outcome,
        actor_user_id=row.actor_user_id,
        actor_token_id=row.actor_token_id,
        workspace_id=row.workspace_id,
        request_id=row.request_id,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
    )


async def list_audit_logs(
    db_manager: DatabaseManager,
    principal: Principal,
    *,
    limit: int = 100,
    action: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
    target_type: str | None = None,
) -> list[AuditLogEntry]:
    if not principal.is_admin:
        raise ForbiddenError("Only admins can list audit logs")
    bounded_limit = min(max(limit, 1), 500)
    async with db_manager.session_factory() as session:
        stmt = (
            select(AuditLogRow)
            # Scope to the admin's own workspace so one tenant cannot read
            # another tenant's audit trail (which includes IPs, user agents, run metadata).
            .where(AuditLogRow.workspace_id == principal.workspace_id)
            .order_by(AuditLogRow.created_at.desc())
            .limit(bounded_limit)
        )
        if actor_user_id:
            stmt = stmt.where(AuditLogRow.actor_user_id == actor_user_id)
        if action:
            stmt = stmt.where(AuditLogRow.action == action)
        if outcome:
            stmt = stmt.where(AuditLogRow.outcome == outcome)
        if actor_token_id:
            stmt = stmt.where(AuditLogRow.actor_token_id == actor_token_id)
        if target_type:
            stmt = stmt.where(AuditLogRow.target_type == target_type)
        rows = (await session.execute(stmt)).scalars()
        return [_audit_log_entry(row) for row in rows]


# The public invoke path records action="agent.invoke" with outcome = run
# status (completed/failed/aborted); the denied path records
# action="agent.invoke.denied". "agent.invoke.completed" covers the console
# invocation path, which uses outcome="success" for completed runs.
_INVOKE_ACTIONS = ("agent.invoke", "agent.invoke.completed", "agent.invoke.denied")


def _stat_type(action: str, outcome: str) -> str:
    if action == "agent.invoke.denied":
        return "denied"
    if action == "agent.invoke.completed":
        return "query" if outcome == "success" else "failed"
    if outcome in ("completed", "success"):
        return "query"
    return "failed"


def build_query_stats(
    rows: list[tuple[str, str, str, str, int, datetime | None]],
    user_labels: dict[str, dict[str, str | None]],
    *,
    days: int,
    now: datetime,
) -> QueryStatsResponse:
    """Assemble dense daily per-user stats from (day, user_id, action, outcome, count, last_at) rows."""
    bounded_days = min(max(days, 1), 365)
    now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=bounded_days - 1)
    day_keys = [(starts_at + timedelta(days=offset)).date().isoformat() for offset in range(bounded_days)]
    active_days = set(day_keys)

    daily_counts: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    last_activity: dict[str, datetime] = {}
    for day, user_id, action, outcome, count, row_last_at in rows:
        if user_id is None or day not in active_days:
            continue
        daily_counts[user_id][day][_stat_type(action, outcome)] += count
        if row_last_at is not None:
            aware = row_last_at if row_last_at.tzinfo is not None else row_last_at.replace(tzinfo=UTC)
            current = last_activity.get(user_id)
            if current is None or aware > current:
                last_activity[user_id] = aware

    users: list[UserQueryStat] = []
    for user_id, by_day in daily_counts.items():
        labels = user_labels.get(user_id, {})
        daily = [
            QueryStatDay(
                date=day,
                query_count=by_day[day]["query"],
                denied_count=by_day[day]["denied"],
                failed_count=by_day[day]["failed"],
            )
            for day in day_keys
        ]
        users.append(
            UserQueryStat(
                user_id=user_id,
                email=labels.get("email"),
                display_name=labels.get("display_name"),
                total_query_count=sum(entry.query_count for entry in daily),
                total_denied_count=sum(entry.denied_count for entry in daily),
                total_failed_count=sum(entry.failed_count for entry in daily),
                last_query_at=last_activity.get(user_id),
                daily=daily,
            )
        )

    users.sort(key=lambda user: (-user.total_query_count, (user.email or user.user_id).lower()))
    return QueryStatsResponse(days=bounded_days, starts_at=starts_at, ends_at=now, users=users)


async def get_user_query_stats(
    db_manager: DatabaseManager,
    principal: Principal,
    *,
    days: int = 30,
) -> QueryStatsResponse:
    if not principal.is_admin:
        raise ForbiddenError("Only admins can view audit query stats")
    bounded_days = min(max(days, 1), 365)
    now = datetime.now(UTC)
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=bounded_days - 1)

    day_expr = func.to_char(func.timezone("UTC", AuditLogRow.created_at), "YYYY-MM-DD").label("day")
    async with db_manager.session_factory() as session:
        grouped = await session.execute(
            select(
                day_expr,
                AuditLogRow.actor_user_id,
                AuditLogRow.action,
                AuditLogRow.outcome,
                func.count().label("event_count"),
                func.max(AuditLogRow.created_at).label("last_at"),
            )
            .where(
                AuditLogRow.workspace_id == principal.workspace_id,
                AuditLogRow.action.in_(_INVOKE_ACTIONS),
                AuditLogRow.created_at >= starts_at,
                AuditLogRow.created_at <= now,
                AuditLogRow.actor_user_id.is_not(None),
            )
            .group_by(day_expr, AuditLogRow.actor_user_id, AuditLogRow.action, AuditLogRow.outcome)
        )
        rows = grouped.all()

        user_ids = {row[1] for row in rows}
        labels: dict[str, dict[str, str | None]] = {}
        if user_ids:
            user_rows = await session.execute(
                select(UserRow.id, UserRow.email, UserRow.display_name).where(UserRow.id.in_(user_ids))
            )
            labels = {
                user_id: {"email": email, "display_name": display_name}
                for user_id, email, display_name in user_rows.all()
            }

    return build_query_stats(rows, labels, days=bounded_days, now=now)
