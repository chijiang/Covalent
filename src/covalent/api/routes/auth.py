"""auth route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request
from fastapi import Response

from covalent.api._auth_helpers import _clear_console_session_cookie
from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._auth_helpers import _set_console_session_cookie
from covalent.api._shared import _record_audit_log
from covalent.application.schemas import ConsoleAccountUpdateRequest
from covalent.application.schemas import ConsoleLoginRequest
from covalent.application.schemas import ConsolePasswordUpdateRequest
from covalent.application.schemas import ConsoleRegisterRequest
from covalent.application.schemas import ConsoleUserResponse
from covalent.application.services.user_service import _authenticate_console_password
from covalent.application.services.user_service import _console_user_response
from covalent.application.services.user_service import _register_console_user
from covalent.application.services.user_service import _update_current_account
from covalent.application.services.user_service import _update_current_password
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

router = APIRouter()


@router.post("/auth/register")
async def register_console_user(
    request: Request,
    response: Response,
    register_request: ConsoleRegisterRequest,
) -> ConsoleUserResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _register_console_user(db_manager, settings, register_request)
    _set_console_session_cookie(response, settings, principal)
    await _record_audit_log(
        db_manager,
        action="auth.register",
        target_type="user",
        target_id=principal.user_id,
        principal=principal,
        request=request,
    )
    return _console_user_response(principal)

@router.post("/auth/login")
async def login_console_user(
    request: Request,
    response: Response,
    login_request: ConsoleLoginRequest,
) -> ConsoleUserResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _authenticate_console_password(db_manager, login_request)
    _set_console_session_cookie(response, settings, principal)
    await _record_audit_log(
        db_manager,
        action="auth.login",
        target_type="user",
        target_id=principal.user_id,
        principal=principal,
        request=request,
    )
    return _console_user_response(principal)

@router.post("/auth/logout")
async def logout_console_user(request: Request, response: Response) -> dict[str, str]:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    try:
        principal = await _resolve_console_principal(request, db_manager)
    except HTTPException:
        principal = None
    _clear_console_session_cookie(response, settings)
    if principal is not None:
        await _record_audit_log(
            db_manager,
            action="auth.logout",
            target_type="user",
            target_id=principal.user_id,
            principal=principal,
            request=request,
        )
    return {"status": "ok"}

@router.get("/me")
async def get_current_console_user(request: Request) -> ConsoleUserResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return _console_user_response(principal)

@router.patch("/account")
async def update_current_console_account(
    request: Request,
    response: Response,
    update_request: ConsoleAccountUpdateRequest,
) -> ConsoleUserResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    updated = await _update_current_account(db_manager, principal, update_request)
    _set_console_session_cookie(response, settings, updated)
    await _record_audit_log(
        db_manager,
        action="account.updated",
        target_type="user",
        target_id=updated.user_id,
        principal=updated,
        request=request,
        metadata={
            "email_changed": updated.email != principal.email,
            "display_name_changed": updated.display_name != principal.display_name,
            "avatar_changed": updated.avatar_url != principal.avatar_url,
            "preferences_changed": updated.preferences != principal.preferences,
        },
    )
    return _console_user_response(updated)

@router.post("/account/password")
async def update_current_console_password(
    request: Request,
    update_request: ConsolePasswordUpdateRequest,
) -> dict[str, str]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    await _update_current_password(db_manager, principal, update_request)
    await _record_audit_log(
        db_manager,
        action="account.password_changed",
        target_type="user",
        target_id=principal.user_id,
        principal=principal,
        request=request,
    )
    return {"status": "ok"}
