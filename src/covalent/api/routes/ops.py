"""ops route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request
import logging
from sqlalchemy import text
from typing import Any

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _augment_sandbox_snapshot, _record_audit_log
from covalent.api.auth import authenticate_api_token
from covalent.application.services.audit_service import (
    AuditLogEntry,
    get_user_query_stats as _get_user_query_stats,
    list_audit_logs as _list_audit_logs,
)
from covalent.application.services.runtime_apply import _reload_runtime_from_database
from covalent.application.schemas import AuditLogResponse, QueryStatsResponse
from covalent.infra.db import DatabaseManager, UserRow
from covalent.infra.memory import SessionStore
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Ops"])


def _audit_dto(e: AuditLogEntry) -> AuditLogResponse:
    return AuditLogResponse(
        id=e.id, actor_user_id=e.actor_user_id, actor_token_id=e.actor_token_id, workspace_id=e.workspace_id,
        action=e.action, target_type=e.target_type, target_id=e.target_id, outcome=e.outcome,
        request_id=e.request_id, ip_address=e.ip_address, user_agent=e.user_agent,
        metadata=dict(e.metadata), created_at=e.created_at,
    )


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
        for skill_name in spm.active_skill_names():
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

@router.post("/v1/ops/reload")
async def reload_runtime_registry(request: Request) -> dict[str, Any]:
    """Rebuild the live registry (agents/MCP/skills) from the database.

    For out-of-band config writers (CLI ``config import`` / ``--reload``) so
    changes become visible without a process restart. The path sits under /v1,
    so it is authenticated by admin-owned API token (Bearer) rather than the
    console cookie.
    """
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await authenticate_api_token(
        request,
        settings=settings,
        session_factory=db_manager.session_factory,
    )
    async with db_manager.session_factory() as session:
        owner = await session.get(UserRow, principal.user_id)
    if owner is None or owner.role != "admin":
        raise HTTPException(status_code=403, detail="Admin token required to reload the runtime registry")

    registry: FrameworkRegistry = request.app.state.registry
    summary = await _reload_runtime_from_database(
        registry,
        request.app.state.config_store,
        settings,
        request.app.state.skill_loader,
        request.app.state.execution_backend,
    )
    try:
        await _record_audit_log(
            db_manager,
            action="runtime.registry.reload",
            target_type="runtime_registry",
            api_principal=principal,
            request=request,
            metadata={"agents": summary.get("agents")},
        )
    except Exception:
        logger.warning("Failed to record runtime registry reload audit log", exc_info=True)
    return {"status": "reloaded", **summary}

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
async def stop_sandbox_session(request: Request, session_id: str) -> dict[str, Any]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can stop sandbox sessions")
    binding_service = getattr(request.app.state, "sandbox_binding_service", None)
    if binding_service is not None:
        # Full binding lifecycle: stop every instance of the session, evict its
        # warm skill processes, and remove instance-private state.
        await binding_service.stop_session(session_id)
        return {"status": "stopped", "session_id": session_id}
    backend = getattr(request.app.state, "execution_backend", None)
    if backend is None:
        raise HTTPException(status_code=404, detail="No execution backend configured")
    await backend.stop(session_id)
    return {"status": "stopped", "session_id": session_id}


def _binding_service(request: Request):
    service = getattr(request.app.state, "sandbox_binding_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="sandbox binding service is unavailable")
    return service


@router.delete("/sandbox/instances/{sandbox_instance_id}")
async def stop_sandbox_instance(request: Request, sandbox_instance_id: str) -> dict[str, Any]:
    """Stop a live instance's container but keep its logical binding, so the
    next run recreates it from the saved spec."""
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can stop sandbox instances")
    stopped = await _binding_service(request).stop_instance(sandbox_instance_id)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"Unknown sandbox instance: {sandbox_instance_id}")
    await _record_audit_log(
        request.app.state.db_manager,
        action="sandbox.instance.stopped",
        target_type="sandbox_instance",
        target_id=sandbox_instance_id,
        principal=principal,
        request=request,
    )
    return {"status": "stopped", "sandbox_instance_id": sandbox_instance_id}


@router.post("/sandbox/instances/{sandbox_instance_id}/reset")
async def reset_sandbox_instance(request: Request, sandbox_instance_id: str) -> dict[str, Any]:
    """Stop the container, evict warm processes, remove private state, and
    delete the logical binding — the next run re-resolves the agent's current
    profile. Conversation history and the shared workspace are untouched."""
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can reset sandbox instances")
    reset = await _binding_service(request).reset_instance(sandbox_instance_id)
    if not reset:
        raise HTTPException(status_code=404, detail=f"Unknown sandbox instance: {sandbox_instance_id}")
    await _record_audit_log(
        request.app.state.db_manager,
        action="sandbox.instance.reset",
        target_type="sandbox_instance",
        target_id=sandbox_instance_id,
        principal=principal,
        request=request,
    )
    return {"status": "reset", "sandbox_instance_id": sandbox_instance_id}

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
    entries = await _list_audit_logs(
        db_manager,
        principal,
        limit=limit,
        action=action,
        outcome=outcome,
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        target_type=target_type,
    )
    return [_audit_dto(e) for e in entries]


@router.get("/audit-logs/query-stats")
async def get_audit_query_stats(request: Request, days: int = 30) -> QueryStatsResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    return await _get_user_query_stats(db_manager, principal, days=days)
