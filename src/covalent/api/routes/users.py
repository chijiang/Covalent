"""users route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import Request

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _record_audit_log
from covalent.api.schemas import ConsoleUserSummaryResponse
from covalent.api.schemas import ConsoleUserUpdateRequest
from covalent.application.services.user_service import _list_console_users
from covalent.application.services.user_service import _update_console_user
from covalent.infra.db import DatabaseManager

router = APIRouter()


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
