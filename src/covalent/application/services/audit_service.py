"""Audit log use cases.

Extracted from ``api._auth_helpers`` into the application layer.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from covalent.api._shared import ConsolePrincipalContext, _audit_log_response
from covalent.api.schemas import AuditLogResponse
from covalent.infra.db import AuditLogRow, DatabaseManager

async def _list_audit_logs(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    *,
    limit: int = 100,
    action: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
    target_type: str | None = None,
) -> list[AuditLogResponse]:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can list audit logs")
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
        return [_audit_log_response(row) for row in rows]
