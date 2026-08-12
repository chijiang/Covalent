"""ops route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request
from sqlalchemy import text
from typing import Any

from covalent.api._auth_helpers import _list_audit_logs
from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _augment_sandbox_snapshot
from covalent.api.schemas import AuditLogResponse
from covalent.infra.db import DatabaseManager
from covalent.infra.memory import SessionStore
from covalent.registry.registry import FrameworkRegistry

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    registry: FrameworkRegistry = request.app.state.registry
    db_manager: DatabaseManager = request.app.state.db_manager
    checks: dict[str, Any] = {"status": "ok", "version": "0.3.0"}

    try:
        async with db_manager.session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "connected"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        checks["status"] = "degraded"

    spm = registry.skill_process_manager
    if spm is not None:
        pool_summaries: dict[str, dict[str, Any]] = {}
        for skill_name in registry.manifest_skills:
            pool = spm._pools.get(skill_name)
            if pool:
                pool_summaries[skill_name] = spm.pool_status(skill_name)
        checks["skill_processes"] = pool_summaries if pool_summaries else "none_active"

    backend = getattr(request.app.state, "execution_backend", None)
    metrics_snapshot = getattr(backend, "metrics_snapshot", None)
    if callable(metrics_snapshot):
        try:
            checks["sandbox"] = metrics_snapshot()
        except Exception as exc:
            checks["sandbox"] = f"error: {exc}"
            checks["status"] = "degraded"

    return checks

@router.get("/sandbox/status")
async def sandbox_status(request: Request) -> dict[str, Any]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can view sandbox status")
    backend = getattr(request.app.state, "execution_backend", None)
    snapshot_fn = getattr(backend, "sandbox_snapshot", None)
    if not callable(snapshot_fn):
        return {"backend": getattr(backend, "name", "unknown"), "supported": False}
    snapshot = await snapshot_fn()
    session_store: SessionStore = request.app.state.session_store
    return await _augment_sandbox_snapshot(snapshot, session_store)

@router.delete("/sandbox/sessions/{session_id}")
async def stop_sandbox_session(request: Request, session_id: str) -> dict[str, str]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can stop sandbox sessions")
    backend = getattr(request.app.state, "execution_backend", None)
    if backend is None:
        raise HTTPException(status_code=404, detail="No execution backend configured")
    await backend.stop(session_id)
    return {"status": "stopped", "session_id": session_id}

@router.get("/audit-logs")
async def list_audit_logs(
    request: Request,
    limit: int = 100,
    action: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
    target_type: str | None = None,
) -> list[AuditLogResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _list_audit_logs(
        db_manager,
        principal,
        limit=limit,
        action=action,
        outcome=outcome,
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        target_type=target_type,
    )
