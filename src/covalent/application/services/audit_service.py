"""Audit log use cases.

Extracted from ``api._auth_helpers`` into the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from covalent.application.errors import ForbiddenError
from covalent.application.principal import Principal
from covalent.infra.db import AuditLogRow, DatabaseManager


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
