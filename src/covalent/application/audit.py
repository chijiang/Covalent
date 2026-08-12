"""Application-layer audit logging.

``record_audit`` writes an audit row. It takes a framework-independent
``RequestMetadata`` (built by the API layer from the HTTP request) instead of a
FastAPI ``Request``, so application services stay framework-independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from covalent.application._utils import _new_chat_item_id
from covalent.application.principal import Principal
from covalent.infra.db import AuditLogRow, DatabaseManager


@dataclass(frozen=True)
class RequestMetadata:
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


async def record_audit(
    db_manager: DatabaseManager,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    outcome: str = "success",
    principal: Principal | None = None,
    api_principal: Any | None = None,
    request_metadata: RequestMetadata | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    rm = request_metadata or RequestMetadata()
    actor_user_id = principal.user_id if principal is not None else getattr(api_principal, "user_id", None)
    actor_token_id = getattr(api_principal, "token_id", None)
    workspace_id = principal.workspace_id if principal is not None else getattr(api_principal, "workspace_id", None)
    async with db_manager.session_factory() as session:
        async with session.begin():
            session.add(
                AuditLogRow(
                    id=_new_chat_item_id("audit"),
                    actor_user_id=actor_user_id,
                    actor_token_id=actor_token_id,
                    workspace_id=workspace_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    outcome=outcome,
                    request_id=rm.request_id,
                    ip_address=rm.ip_address,
                    user_agent=rm.user_agent,
                    metadata_json=dict(metadata or {}),
                )
            )
