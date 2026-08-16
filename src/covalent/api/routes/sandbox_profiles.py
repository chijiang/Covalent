"""Sandbox profile administration routes.

Thin HTTP layer over the application ``SandboxProfileService``: authentication,
admin authority, and exception/DTO mapping only. Profile mutations are
admin-only; reads are scoped to the principal's workspace (a workspace-owned
profile is never leaked to another workspace's member).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _record_audit_log
from covalent.application.schemas import SandboxProfileCreateRequest, SandboxProfileUpdateRequest
from covalent.infra.db import DatabaseManager

router = APIRouter()


def _profile_service(request: Request):
    service = getattr(request.app.state, "sandbox_profile_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="sandbox profile service is unavailable")
    return service


async def _require_admin(request: Request, db_manager: DatabaseManager):
    principal = await _resolve_console_principal(request, db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can manage sandbox profiles")
    return principal


async def _audit(request: Request, db_manager: DatabaseManager, principal: Any, action: str, target_id: str | None, outcome: str = "success") -> None:
    try:
        await _record_audit_log(
            db_manager,
            action=action,
            target_type="sandbox_profile",
            target_id=target_id,
            outcome=outcome,
            principal=principal,
            request=request,
        )
    except Exception:
        # Audit failure must not fail the mutation itself.
        pass


@router.get("/sandbox/profiles")
async def list_profiles(request: Request) -> list[dict[str, Any]]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _profile_service(request).profile_responses(workspace_id=principal.workspace_id)


@router.get("/sandbox/profiles/{profile_id}")
async def get_profile(request: Request, profile_id: str) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _profile_service(request).profile_response(profile_id, workspace_id=principal.workspace_id)


@router.post("/sandbox/profiles")
async def create_profile(request: Request, body: SandboxProfileCreateRequest) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    profile = await _profile_service(request).create_profile(body, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.created", profile["id"])
    return profile


@router.put("/sandbox/profiles/{profile_id}")
async def update_profile(request: Request, profile_id: str, body: SandboxProfileUpdateRequest) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    profile = await _profile_service(request).update_profile(profile_id, body, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.updated", profile_id)
    return profile


@router.post("/sandbox/profiles/{profile_id}/validate")
async def validate_profile(request: Request, profile_id: str) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    profile = await _profile_service(request).validate_profile(profile_id, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.validated", profile_id)
    return profile


@router.post("/sandbox/profiles/{profile_id}/enable")
async def enable_profile(request: Request, profile_id: str) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    profile = await _profile_service(request).enable_profile(profile_id, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.enabled", profile_id)
    return profile


@router.post("/sandbox/profiles/{profile_id}/disable")
async def disable_profile(request: Request, profile_id: str) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    profile = await _profile_service(request).disable_profile(profile_id, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.disabled", profile_id)
    return profile


@router.delete("/sandbox/profiles/{profile_id}")
async def delete_profile(request: Request, profile_id: str) -> dict[str, Any]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _require_admin(request, db_manager)
    await _profile_service(request).delete_profile(profile_id, workspace_id=principal.workspace_id)
    await _audit(request, db_manager, principal, "sandbox.profile.deleted", profile_id)
    return {"status": "deleted", "id": profile_id}
