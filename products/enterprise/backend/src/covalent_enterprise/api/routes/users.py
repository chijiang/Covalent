"""users route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import Request

from covalent_enterprise.api._auth_helpers import _resolve_console_principal
from covalent_enterprise.api._shared import _record_audit_log
from covalent_enterprise.application.schemas import ConsoleUserSummaryResponse
from covalent_enterprise.application.schemas import ConsoleUserUpdateRequest
from covalent_enterprise.application.services.user_service import _list_console_users
from covalent_enterprise.application.services.user_service import _update_console_user
from covalent_enterprise.infra.db import DatabaseManager

router = APIRouter(tags=["Users"])


@router.get("/users")
async def list_console_users(request: Request) -> list[ConsoleUserSummaryResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _list_console_users(db_manager, principal)

@router.patch("/users/{user_id}")
async def update_console_user(
    request: Request,
    user_id: str,
    update_request: ConsoleUserUpdateRequest,
) -> ConsoleUserSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    updated = await _update_console_user(db_manager, principal, user_id, update_request)
    await _record_audit_log(
        db_manager,
        action="user.updated",
        target_type="user",
        target_id=updated.user_id,
        principal=principal,
        request=request,
        metadata={"role": updated.role, "status": updated.status, "workspace_role": updated.workspace_role},
    )
    return updated
