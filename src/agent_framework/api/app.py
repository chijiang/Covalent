from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import functools
import json
import logging
import os
import mimetypes
from pathlib import Path
import shutil
import tempfile
from time import perf_counter
from typing import Any, Literal
import zipfile

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import ValidationError

from agent_framework.api.schemas import (
    AgentRunRequest,
    AgentRunResponse,
    AgentRunLogResponse,
    AuditLogResponse,
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenSummaryResponse,
    ApiTokenUpdateRequest,
    ApiTokenUsageResponse,
    ConsoleAccountUpdateRequest,
    ConsoleLoginRequest,
    ConsolePasswordUpdateRequest,
    ConsoleRegisterRequest,
    ConsoleUserResponse,
    ConsoleUserSummaryResponse,
    ConsoleUserUpdateRequest,
    LocalToolSummaryResponse,
    AgentSummaryResponse,
    AttachmentUploadResponse,
    AttachmentUploadItemResponse,
    ChatSessionResponse,
    ChatSessionSummaryResponse,
    ChatSessionUpdateRequest,
    ConfigDocumentResponse,
    ConfigDocumentUpdateRequest,
    ManagementExportResponse,
    ManagementImportResponse,
    McpInspectRequest,
    McpInspectResponse,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolSummaryResponse,
    PublicationRequestResponse,
    PublicationReviewRequest,
    PublicAgentInvokeRequest,
    PublicAgentInvokeResponse,
    SkillInstallRequest,
    SkillInstallResponse,
    SkillPreviewFileResponse,
    SkillPreviewResponse,
    SkillSummaryResponse,
)
from agent_framework.api.auth import (
    authenticate_api_token,
    require_agent_allowed,
    require_memory_mode_allowed,
    require_scope,
    require_trace_level_allowed,
)
from agent_framework.core.attachment_processing import process_attachment_bytes
from agent_framework.core.types import RunContext
from agent_framework.infra.config_store import ConfigStore, PersistedSkillSourceConfig
from sqlalchemy import text

from agent_framework.infra.db import (
    DatabaseManager,
)
from agent_framework.infra.migrations import run_database_migrations
from agent_framework.infra.memory import (
    ChatActivityItem,
    ChatSessionRecord,
    PersistentSessionStore,
    SessionStore,
)
from agent_framework.infra.settings import AppSettings
from agent_framework.mcp.client import McpSdkClient
from agent_framework.model.base import ModelProviderError
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.react import ReactAgentRuntime
from agent_framework.runtime.backend import make_backend
from agent_framework.skills.loader import SkillLoader, normalize_git_source_payload

from agent_framework.api._shared import (
    _augment_sandbox_snapshot,
    _coerce_int,
    _new_chat_item_id,
    _payload_text,
    _record_audit_log,
    _record_sandbox_session,
    _rmtree_async,
    _safe_extract_zip,
    _sandbox_reaper_loop,
    to_agent_summary,
    to_chat_session_response,
    to_chat_session_summary_response,
)

from agent_framework.api._auth_helpers import (
    ConsoleAuthGuardMiddleware,
    _authenticate_console_password,
    _clear_console_session_cookie,
    _console_user_response,
    _get_api_token_usage,
    _list_api_token_runs,
    _list_api_token_summaries,
    _list_audit_logs,
    _list_console_users,
    _register_console_user,
    _resolve_console_principal,
    _seed_initial_admin_user,
    _set_console_session_cookie,
    _update_console_user,
    _update_current_account,
    _update_current_password,
)
from agent_framework.application.services.token_service import (
    _create_api_token,
    _revoke_api_token,
    _update_api_token,
)

from agent_framework.api._session_helpers import (
    _append_assistant_attachments,
    _attachment_session_dir,
    _build_resume_tool_result,
    _build_session_preview,
    _build_user_transcript_message,
    _chat_upload_session_dir,
    _chat_upload_visible_root,
    _download_session_dir,
    _extract_pending_user_input,
    _generate_session_title,
    _next_available_upload_path,
    _payload_output_text,
    _published_download_attachments_from_tool_results,
    _replace_assistant_transcript,
    _safe_uploaded_filename,
    _upsert_assistant_transcript,
)

from agent_framework.api._public_invoke_helpers import (
    _ApiTokenRunLimiter,
    _encode_public_sse,
    _enforce_api_token_policy_limits,
    _public_run_completed_payload,
    _public_stream_events,
    _record_denied_public_agent_invoke,
    _record_public_agent_run,
    _resolve_public_invoke_session_id,
    _usage_payload,
)

from agent_framework.api._config_helpers import (
    _available_local_tool_summaries,
    _build_management_export_payload,
    _config_document_response,
    _ensure_api_principal_can_invoke_agent,
    _ensure_console_principal_can_access_agent,
    _ensure_console_principal_can_access_mcp_server,
    _ensure_console_principal_can_access_session,
    _extract_agent_renames,
    _import_management_payload,
    _normalize_config_kind,
    _normalize_management_export_format,
    _normalize_management_kind,
    _request_resource_publication,
    _resolve_api_agent_name,
    _resolve_console_agent_name,
    _review_resource_publication,
    _serialize_management_export_payload,
    _validate_config_payload,
    build_registry,
)
from agent_framework.api._skill_helpers import (
    _build_skill_export_zip,
    _can_access_manifest_skill,
    _collect_skill_preview_files,
    _detect_uploaded_skill_directory,
    _ensure_skill_access,
    _ensure_skill_state_mutation_allowed,
    _find_matching_git_skill,
    _infer_skill_install_source_type,
    _inline_skill_summary_response,
    _manifest_skill_summary_response,
    _matches_skill_source,
    _reconcile_skill_process_manager,
    _resolve_skill_preview_paths,
    _set_skill_enabled,
    _skill_category,
    _sync_registry_skill_states,
    _visible_skill_source_payload_for_spec,
)
from agent_framework.api._runtime_apply import _apply_runtime_config

logger = logging.getLogger(__name__)


SSE_EVENT_ASSISTANT = "assistant"
SSE_EVENT_FINAL = "final"
SSE_EVENT_TOOL_CALLS = "tool_calls"
SSE_EVENT_TOOL_RESULTS = "tool_results"
SSE_EVENT_ITERATION = "iteration"
SSE_EVENT_THOUGHT = "thought"
SSE_EVENT_ERROR = "error"
SSE_EVENT_INPUT_REQUIRED = "input_required"
SSE_EVENT_INPUT_RESOLVED = "input_resolved"
SSE_EVENT_SESSION = "session"
SSE_EVENT_CONTEXT_WINDOW = "context_window"
SSE_EVENT_MODEL_CALL = "model_call"
SSE_EVENT_DELEGATE_PREFIX = "delegate_"
SSE_EVENT_DELEGATE_ASSISTANT = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_ASSISTANT}"
SSE_EVENT_DELEGATE_FINAL = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_FINAL}"
SSE_EVENT_DELEGATE_TOOL_CALLS = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_TOOL_CALLS}"
SSE_EVENT_DELEGATE_TOOL_RESULTS = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_TOOL_RESULTS}"
SSE_EVENT_DELEGATE_ITERATION = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_ITERATION}"
SSE_EVENT_DELEGATE_THOUGHT = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_THOUGHT}"
SSE_EVENT_DELEGATE_ERROR = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_ERROR}"
SSE_EVENT_DELEGATE_INPUT_REQUIRED = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_INPUT_REQUIRED}"
SSE_EVENT_DELEGATE_CONTEXT_WINDOW = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_CONTEXT_WINDOW}"
SSE_EVENT_DELEGATE_MODEL_CALL = f"{SSE_EVENT_DELEGATE_PREFIX}{SSE_EVENT_MODEL_CALL}"

DELEGATE_TOOL_PREFIX = "agent__"

TRACE_ACTIVITY_EVENTS = {
    SSE_EVENT_TOOL_CALLS,
    SSE_EVENT_TOOL_RESULTS,
    SSE_EVENT_ITERATION,
    SSE_EVENT_THOUGHT,
    SSE_EVENT_ERROR,
    SSE_EVENT_INPUT_REQUIRED,
    SSE_EVENT_CONTEXT_WINDOW,
    SSE_EVENT_MODEL_CALL,
    SSE_EVENT_DELEGATE_ASSISTANT,
    SSE_EVENT_DELEGATE_FINAL,
    SSE_EVENT_DELEGATE_TOOL_CALLS,
    SSE_EVENT_DELEGATE_TOOL_RESULTS,
    SSE_EVENT_DELEGATE_ITERATION,
    SSE_EVENT_DELEGATE_THOUGHT,
    SSE_EVENT_DELEGATE_ERROR,
    SSE_EVENT_DELEGATE_INPUT_REQUIRED,
    SSE_EVENT_DELEGATE_CONTEXT_WINDOW,
    SSE_EVENT_DELEGATE_MODEL_CALL,
}



@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppSettings()
    settings.validate_runtime_secrets()
    if (
        settings.is_dev_auth_mode() is False
        and (settings.console_auth_mode or "local").strip().lower() in {"trusted_header", "trusted-headers", "headers"}
        and not (settings.console_trusted_header_secret or "").strip()
    ):
        logger.warning(
            "console_auth_mode=trusted_header is running WITHOUT a signature secret "
            "(AGENT_FRAMEWORK_CONSOLE_TRUSTED_HEADER_SECRET is unset). Any client "
            "that can reach the app directly can forge admin identity via headers. "
            "Configure the secret and have your reverse proxy sign the identity headers."
        )
    if (
        settings.is_dev_auth_mode() is False
        and (settings.execution_backend_kind or "filesystem").strip().lower() == "filesystem"
    ):
        logger.warning(
            "execution_backend_kind=filesystem provides NO OS-level isolation. Skills run as "
            "local subprocesses with the backend process's full filesystem/network reach, and "
            "the in-skill PermissionGuard only patches builtins.open (trivially bypassed). "
            "Only run trusted skill code in this mode; set EXECUTION_BACKEND_KIND=docker for "
            "untrusted or third-party skills."
        )
    database_url = settings.database_url
    if not database_url:
        raise RuntimeError("AGENT_FRAMEWORK_DATABASE_URL must be set when using persistent config storage")
    await anyio.to_thread.run_sync(run_database_migrations, database_url.replace('+asyncpg', ''))
    db_manager = DatabaseManager(database_url)
    await _seed_initial_admin_user(db_manager, settings)
    config_store = ConfigStore(db_manager.session_factory)
    # The execution backend must exist before build_registry so the skill
    # meta-tools (run_skill_script) can route through it. The skill-source-dirs
    # provider is a closure over the registry, assigned right after build_registry.
    registry_holder: dict[str, Any] = {}

    def _skill_source_dirs() -> list[str]:
        registry = registry_holder.get("registry")
        if registry is None:
            return []
        return [
            spec.source_dir
            for spec in registry.manifest_skills.values()
            if getattr(spec, "source_dir", None)
        ]

    execution_backend = make_backend(settings, skill_source_dirs_provider=_skill_source_dirs)
    registry, loader, skill_source_payload = await build_registry(
        settings, config_store, backend=execution_backend
    )
    registry_holder["registry"] = registry

    git_skills = await loader.discover_git(skill_source_payload)
    for spec in git_skills:
        registry.register_manifest_skill(spec)
        logger.info("Loaded git skill '%s' (v%s) from %s", spec.name, spec.version, spec.source_dir)

    await _sync_registry_skill_states(registry, config_store)
    await _reconcile_skill_process_manager(registry, execution_backend)

    app.state.settings = settings
    app.state.execution_backend = execution_backend
    app.state.db_manager = db_manager
    app.state.config_store = config_store
    app.state.registry = registry
    app.state.skill_loader = loader
    app.state.session_store = PersistentSessionStore(db_manager.session_factory)
    app.state.api_token_run_limiter = _ApiTokenRunLimiter(settings.api_token_max_concurrent_runs)

    # Reclaim sandbox containers orphaned by a previous run, then start a periodic
    # reaper that removes containers whose session has been deleted. No-op for the
    # FileSystem backend (list_sandbox_sessions returns []).
    await execution_backend.startup_sweep()
    reaper_task = asyncio.create_task(
        _sandbox_reaper_loop(
            execution_backend,
            app.state.session_store,
            settings.execution_backend_docker_reaper_interval_seconds,
            settings.execution_backend_docker_idle_timeout_seconds,
        )
    )

    app.state.runtime = ReactAgentRuntime(
        registry,
        session_store=app.state.session_store,
        session_history_limit=settings.session_history_limit,
        context_token_budget=settings.context_token_budget,
        context_compact_threshold=settings.context_compact_threshold,
        context_summary_model=settings.context_summary_model,
        enable_llm_summarization=settings.enable_llm_summarization,
    )

    yield
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    await registry.aclose()
    await execution_backend.aclose()
    await db_manager.dispose()










def create_app() -> FastAPI:
    app = FastAPI(title="Covalent", version="0.3.0", lifespan=lifespan)
    app.add_middleware(ConsoleAuthGuardMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        registry: FrameworkRegistry = app.state.registry
        db_manager: DatabaseManager = app.state.db_manager
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

        backend = getattr(app.state, "execution_backend", None)
        metrics_snapshot = getattr(backend, "metrics_snapshot", None)
        if callable(metrics_snapshot):
            try:
                checks["sandbox"] = metrics_snapshot()
            except Exception as exc:
                checks["sandbox"] = f"error: {exc}"
                checks["status"] = "degraded"

        return checks

    @app.get("/sandbox/status")
    async def sandbox_status(request: Request) -> dict[str, Any]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="Only admins can view sandbox status")
        backend = getattr(app.state, "execution_backend", None)
        snapshot_fn = getattr(backend, "sandbox_snapshot", None)
        if not callable(snapshot_fn):
            return {"backend": getattr(backend, "name", "unknown"), "supported": False}
        snapshot = await snapshot_fn()
        session_store: SessionStore = app.state.session_store
        return await _augment_sandbox_snapshot(snapshot, session_store)

    @app.delete("/sandbox/sessions/{session_id}")
    async def stop_sandbox_session(request: Request, session_id: str) -> dict[str, str]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="Only admins can stop sandbox sessions")
        backend = getattr(app.state, "execution_backend", None)
        if backend is None:
            raise HTTPException(status_code=404, detail="No execution backend configured")
        await backend.stop(session_id)
        return {"status": "stopped", "session_id": session_id}

    @app.get("/me")
    async def get_current_console_user(request: Request) -> ConsoleUserResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return _console_user_response(principal)

    @app.patch("/account")
    async def update_current_console_account(
        request: Request,
        response: Response,
        update_request: ConsoleAccountUpdateRequest,
    ) -> ConsoleUserResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.post("/account/password")
    async def update_current_console_password(
        request: Request,
        update_request: ConsolePasswordUpdateRequest,
    ) -> dict[str, str]:
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.post("/auth/register")
    async def register_console_user(
        request: Request,
        response: Response,
        register_request: ConsoleRegisterRequest,
    ) -> ConsoleUserResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.post("/auth/login")
    async def login_console_user(
        request: Request,
        response: Response,
        login_request: ConsoleLoginRequest,
    ) -> ConsoleUserResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.post("/auth/logout")
    async def logout_console_user(request: Request, response: Response) -> dict[str, str]:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.get("/users")
    async def list_console_users(request: Request) -> list[ConsoleUserSummaryResponse]:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _list_console_users(db_manager, principal)

    @app.patch("/users/{user_id}")
    async def update_console_user(
        request: Request,
        user_id: str,
        update_request: ConsoleUserUpdateRequest,
    ) -> ConsoleUserSummaryResponse:
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.get("/api-tokens")
    async def list_api_tokens(request: Request) -> list[ApiTokenSummaryResponse]:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _list_api_token_summaries(db_manager, principal)

    @app.post("/api-tokens")
    async def create_api_token(request: Request, token_request: ApiTokenCreateRequest) -> ApiTokenCreateResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _create_api_token(db_manager, settings, token_request, principal, request)

    @app.get("/api-tokens/usage")
    async def get_api_token_usage(request: Request, days: int = 30) -> ApiTokenUsageResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _get_api_token_usage(db_manager, principal, days=days)

    @app.patch("/api-tokens/{token_id}")
    async def update_api_token(
        request: Request,
        token_id: str,
        token_request: ApiTokenUpdateRequest,
    ) -> ApiTokenSummaryResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _update_api_token(db_manager, token_id, token_request, principal, request)

    @app.delete("/api-tokens/{token_id}")
    async def revoke_api_token(request: Request, token_id: str) -> ApiTokenSummaryResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _revoke_api_token(db_manager, token_id, principal, request)

    @app.get("/api-tokens/{token_id}/runs")
    async def list_api_token_runs(request: Request, token_id: str, limit: int = 50) -> list[AgentRunLogResponse]:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return await _list_api_token_runs(db_manager, token_id, principal, limit=limit)

    @app.get("/audit-logs")
    async def list_audit_logs(
        request: Request,
        limit: int = 100,
        action: str | None = None,
        outcome: str | None = None,
        actor_user_id: str | None = None,
        actor_token_id: str | None = None,
        target_type: str | None = None,
    ) -> list[AuditLogResponse]:
        db_manager: DatabaseManager = app.state.db_manager
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

    @app.post("/v1/agent/invoke", response_model=None)
    async def public_invoke_agent(request: Request, invoke_request: PublicAgentInvokeRequest) -> PublicAgentInvokeResponse | StreamingResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime

        memory_mode = invoke_request.memory.mode
        trace_level = invoke_request.trace.level
        try:
            principal = await authenticate_api_token(
                request,
                settings=settings,
                session_factory=db_manager.session_factory,
            )
        except HTTPException as exc:
            await _record_denied_public_agent_invoke(
                db_manager,
                principal=None,
                agent_name=invoke_request.agent,
                memory_mode=memory_mode,
                request=request,
                reason=str(exc.detail),
                status_code=exc.status_code,
            )
            raise
        resolved_agent_name = invoke_request.agent
        try:
            require_scope(principal, "agent:invoke")
            require_agent_allowed(principal, invoke_request.agent)
            require_memory_mode_allowed(principal, memory_mode)
            require_trace_level_allowed(principal, trace_level)
            resolved_agent_name = await _resolve_api_agent_name(db_manager, principal, invoke_request.agent)
            await _ensure_api_principal_can_invoke_agent(db_manager, principal, resolved_agent_name)
            await _enforce_api_token_policy_limits(db_manager, principal, agent_name=resolved_agent_name)
        except HTTPException as exc:
            await _record_denied_public_agent_invoke(
                db_manager,
                principal=principal,
                agent_name=resolved_agent_name,
                memory_mode=memory_mode,
                request=request,
                reason=str(exc.detail),
                status_code=exc.status_code,
            )
            raise

        try:
            agent = registry.get_agent(resolved_agent_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {invoke_request.agent}") from exc

        run_id = _new_chat_item_id("run")
        created_at = datetime.now(UTC)
        session_id = await _resolve_public_invoke_session_id(
            db_manager,
            principal,
            memory_mode=memory_mode,
            requested_session_id=invoke_request.memory.session_id,
            run_id=run_id,
        )
        context = RunContext(
            agent_name=agent.name,
            session_id=session_id or run_id,
            metadata={
                **(invoke_request.metadata or {}),
                "memory_mode": memory_mode,
                "run_id": run_id,
                "principal": {
                    "user_id": principal.user_id,
                    "workspace_id": principal.workspace_id,
                    "token_id": principal.token_id,
                },
            },
            execution_backend=getattr(app.state, "execution_backend", None),
        )

        # Record sandbox metadata before the first container is created
        # (agent name for monitoring; outbound affects network mode).
        _record_sandbox_session(getattr(app.state, "execution_backend", None), context.session_id or "", agent)

        if invoke_request.stream:
            limiter: _ApiTokenRunLimiter | None = getattr(app.state, "api_token_run_limiter", None)

            async def event_stream():
                # Acquire the per-token slot inside the generator so the slot is
                # held only while the stream is actually consuming resources, and
                # released in finally even if the client disconnects mid-stream.
                if limiter is not None:
                    await limiter.acquire(principal.token_id)
                started = perf_counter()
                final_payload: dict[str, Any] | None = None
                error_payload: dict[str, Any] = {}
                status = "completed"
                yield _encode_public_sse(
                    "run.created",
                    {
                        "run_id": run_id,
                        "agent": agent.name,
                        "memory_mode": memory_mode,
                        "session_id": session_id,
                        "created_at": created_at.isoformat(),
                    },
                )
                try:
                    async for event in runtime.stream_events(agent, invoke_request.input, context):
                        event_name = str(event.get("event") or "")
                        payload = event.get("payload")
                        if event_name == "final" and isinstance(payload, dict):
                            final_payload = payload
                        for public_event in _public_stream_events(event_name, payload, trace_level=trace_level):
                            yield public_event
                except ModelProviderError as exc:
                    status = "failed"
                    status_code = 502 if exc.status_code is None else min(max(exc.status_code, 400), 599)
                    error_payload = {"code": "model_error", "status_code": status_code, "message": exc.detail}
                    yield _encode_public_sse("run.failed", {"run_id": run_id, "error": error_payload})
                except Exception as exc:
                    logger.exception("Public agent invoke stream failed", extra={"agent_name": agent.name, "run_id": run_id})
                    status = "failed"
                    error_payload = {"code": "internal_error", "message": str(exc) or "Agent run failed unexpectedly."}
                    yield _encode_public_sse("run.failed", {"run_id": run_id, "error": error_payload})
                finally:
                    latency_ms = int((perf_counter() - started) * 1000)
                    if final_payload is not None:
                        yield _encode_public_sse(
                            "run.completed",
                            _public_run_completed_payload(
                                run_id=run_id,
                                agent_name=agent.name,
                                memory_mode=memory_mode,
                                session_id=session_id,
                                final_payload=final_payload,
                            ),
                        )
                    await _record_public_agent_run(
                        db_manager,
                        principal=principal,
                        run_id=run_id,
                        agent_name=agent.name,
                        memory_mode=memory_mode,
                        session_id=session_id,
                        status=status,
                        latency_ms=latency_ms,
                        provider=agent.provider.provider,
                        model=agent.provider.model,
                        usage=_usage_payload(final_payload),
                        error=error_payload,
                        metadata=invoke_request.metadata,
                    )
                    if limiter is not None:
                        await limiter.release(principal.token_id)

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        limiter: _ApiTokenRunLimiter | None = getattr(app.state, "api_token_run_limiter", None)
        if limiter is not None:
            await limiter.acquire(principal.token_id)
        started = perf_counter()
        try:
            result = await runtime.run(agent, invoke_request.input, context)
        except ModelProviderError as exc:
            latency_ms = int((perf_counter() - started) * 1000)
            status_code = 502 if exc.status_code is None else min(max(exc.status_code, 400), 599)
            await _record_public_agent_run(
                db_manager,
                principal=principal,
                run_id=run_id,
                agent_name=agent.name,
                memory_mode=memory_mode,
                session_id=session_id,
                status="failed",
                latency_ms=latency_ms,
                provider=agent.provider.provider,
                model=agent.provider.model,
                usage={},
                error={"code": "model_error", "status_code": status_code, "message": exc.detail},
                metadata=invoke_request.metadata,
            )
            raise HTTPException(status_code=status_code, detail=exc.detail) from exc
        finally:
            if limiter is not None:
                await limiter.release(principal.token_id)

        latency_ms = int((perf_counter() - started) * 1000)
        usage = result.usage.model_dump(mode="json") if result.usage is not None else {}
        await _record_public_agent_run(
            db_manager,
            principal=principal,
            run_id=run_id,
            agent_name=agent.name,
            memory_mode=memory_mode,
            session_id=session_id,
            status="completed",
            latency_ms=latency_ms,
            provider=agent.provider.provider,
            model=agent.provider.model,
            usage=usage,
            error={},
            metadata=invoke_request.metadata,
        )
        return PublicAgentInvokeResponse(
            id=run_id,
            agent=agent.name,
            memory_mode=memory_mode,
            session_id=session_id,
            output_text=result.output_text,
            tool_calls=[tool_call.model_dump(mode="json") for tool_call in result.tool_calls],
            metadata={"provider": agent.provider.provider, "model": agent.provider.model},
            usage=usage,
            created_at=created_at,
        )

    @app.get("/providers/{provider_name}/models")
    async def list_provider_models(request: Request, provider_name: str) -> list[str]:
        """Fetch available models from an OpenAI-compatible provider."""
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        providers = await config_store.get_document("providers", principal.config)
        target = None
        for p in providers:
            if p.get("name") == provider_name or p.get("internal_name") == provider_name:
                target = p
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
        base_url = str(target.get("base_url", ""))
        api_key = target.get("api_key")
        if not base_url or not api_key:
            raise HTTPException(status_code=400, detail="Provider missing base_url or api_key")
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=settings.request_timeout_seconds,
            )
            result = await client.models.list()
            models = sorted([m.id for m in result.data if m.id])
            return models
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch models: {exc}") from exc

    @app.get("/sessions")
    async def list_sessions(request: Request) -> list[ChatSessionSummaryResponse]:
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        filters = {} if principal.is_admin else {"owner_user_id": principal.user_id, "workspace_id": principal.workspace_id}
        return [to_chat_session_summary_response(record) for record in await session_store.list_sessions(**filters)]

    @app.get("/sessions/{session_id}")
    async def get_session(request: Request, session_id: str) -> ChatSessionResponse:
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        record = await session_store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        _ensure_console_principal_can_access_session(principal, record)
        return to_chat_session_response(record)

    @app.patch("/sessions/{session_id}")
    async def rename_session(request: Request, session_id: str, update_request: ChatSessionUpdateRequest) -> ChatSessionResponse:
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        existing = await session_store.get_session(session_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        _ensure_console_principal_can_access_session(principal, existing)
        try:
            record = await session_store.update_title(session_id, update_request.title.strip(), "manual")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        return to_chat_session_response(record)

    @app.delete("/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str) -> dict[str, str]:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        existing = await session_store.get_session(session_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        _ensure_console_principal_can_access_session(principal, existing)
        if not await session_store.delete_session(session_id):
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")

        async def _remove_workspace_dir() -> None:
            if settings.session_workspace_enabled:
                await _rmtree_async(settings.session_workspace_dir(session_id), ignore_errors=True)

        # Run the four filesystem cleanups concurrently off the event loop.
        await asyncio.gather(
            _rmtree_async(_attachment_session_dir(settings.workspace_root(), session_id), ignore_errors=True),
            _rmtree_async(_chat_upload_session_dir(settings, session_id), ignore_errors=True),
            _rmtree_async(_download_session_dir(settings.workspace_root(), session_id), ignore_errors=True),
            _remove_workspace_dir(),
        )
        await app.state.execution_backend.stop(session_id)
        return {"status": "deleted", "id": session_id}

    @app.post("/attachments/upload")
    async def upload_attachments(
        request: Request,
        session_id: str = Form(...),
        delivery_mode: Literal["parse", "workspace"] = Form("parse"),
        metadata_json: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ) -> AttachmentUploadResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        existing_session = await session_store.get_session(session_id)
        if existing_session is not None:
            _ensure_console_principal_can_access_session(principal, existing_session)
        if not files:
            raise HTTPException(status_code=400, detail="At least one attachment is required")
        try:
            metadata_items = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="metadata_json must be valid JSON") from exc
        if not isinstance(metadata_items, list):
            raise HTTPException(status_code=400, detail="metadata_json must be a JSON array")

        workspace_root = _chat_upload_visible_root(settings, session_id)
        target_dir = _chat_upload_session_dir(settings, session_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        uploaded_files: list[AttachmentUploadItemResponse] = []
        for index, file in enumerate(files, start=1):
            metadata = metadata_items[index - 1] if index - 1 < len(metadata_items) and isinstance(metadata_items[index - 1], dict) else {}
            safe_name = _safe_uploaded_filename(file.filename or "", f"attachment-{index}")
            target_path = _next_available_upload_path(target_dir, safe_name)
            content = await file.read()
            if len(content) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File '{safe_name}' exceeds maximum upload size ({settings.max_upload_bytes} bytes)",
                )
            target_path.write_bytes(content)
            content_type = str(file.content_type or metadata.get("type") or "application/octet-stream")
            workspace_path = target_path.relative_to(workspace_root).as_posix()
            try:
                processed = process_attachment_bytes(
                    file_name=target_path.name,
                    content_type=content_type,
                    raw_bytes=content,
                    workspace_path=workspace_path,
                    delivery_mode=delivery_mode,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            uploaded_files.append(
                AttachmentUploadItemResponse(
                    name=target_path.name,
                    size=len(content),
                    content_type=content_type,
                    last_modified=_coerce_int(metadata.get("lastModified") or metadata.get("last_modified")),
                    workspace_path=workspace_path,
                    uploaded_at=datetime.now(UTC),
                    delivery_mode=delivery_mode,
                    kind=str(processed["kind"]),
                    summary=str(processed["summary"]),
                    model_prompt_text=str(processed["model_prompt_text"]),
                    model_content=processed["model_content"] if isinstance(processed.get("model_content"), list) else [],
                    page_count=int(processed["page_count"]) if processed["page_count"] is not None else None,
                )
            )

        return AttachmentUploadResponse(session_id=session_id, files=uploaded_files)

    @app.get("/downloads/{session_id}/{file_name}")
    async def download_published_file(request: Request, session_id: str, file_name: str) -> FileResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        existing_session = await session_store.get_session(session_id)
        if existing_session is None:
            raise HTTPException(status_code=404, detail="Unknown download")
        _ensure_console_principal_can_access_session(principal, existing_session)
        normalized_name = Path(file_name).name
        if not normalized_name or normalized_name != file_name:
            raise HTTPException(status_code=404, detail="Unknown download")

        target_path = _download_session_dir(settings.workspace_root(), session_id) / normalized_name
        if not target_path.is_file():
            raise HTTPException(status_code=404, detail="Unknown download")

        media_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
        return FileResponse(target_path, media_type=media_type, filename=target_path.name)

    @app.get("/agents")
    async def list_agents(request: Request) -> list[dict[str, str]]:
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        agents = await config_store.get_document("agents", principal.config)
        return [
            {"name": str(agent.get("name") or ""), "description": str(agent.get("description") or "")}
            for agent in agents
            if str(agent.get("name") or "") and agent.get("enabled", True) is not False
        ]

    @app.get("/local-tools")
    async def list_local_tools(request: Request) -> list[LocalToolSummaryResponse]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        return _available_local_tool_summaries(registry, settings)

    @app.get("/agents/{agent_name}")
    async def get_agent(request: Request, agent_name: str) -> AgentSummaryResponse:
        db_manager: DatabaseManager = app.state.db_manager
        registry: FrameworkRegistry = app.state.registry
        principal = await _resolve_console_principal(request, db_manager)
        resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
        await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
        try:
            summary = to_agent_summary(registry.get_agent(resolved_agent_name))
            return summary.model_copy(update={"name": agent_name})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

    @app.post("/agents/{agent_name}/run")
    async def run_agent(request: Request, agent_name: str, run_request: AgentRunRequest) -> AgentRunResponse:
        db_manager: DatabaseManager = app.state.db_manager
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime
        principal = await _resolve_console_principal(request, db_manager)
        resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
        await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
        session_id = run_request.session_id or _new_chat_item_id("session")
        # Record sandbox metadata before the container is created.
        try:
            agent = registry.get_agent(resolved_agent_name)
            _record_sandbox_session(getattr(app.state, "execution_backend", None), session_id, agent)
        except Exception:
            logger.debug("Failed to record sandbox session metadata for %s", session_id, exc_info=True)
        try:
            agent = registry.get_agent(resolved_agent_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

        try:
            result = await runtime.run(
                agent,
                run_request.input,
                RunContext(agent_name=resolved_agent_name, session_id=session_id, metadata=run_request.metadata, execution_backend=getattr(app.state, "execution_backend", None)),
            )
        except ModelProviderError as exc:
            status_code = 502 if exc.status_code is None else min(max(exc.status_code, 400), 599)
            raise HTTPException(status_code=status_code, detail=exc.detail) from exc

        return AgentRunResponse(
            agent=agent_name,
            output_text=result.output_text,
            tool_calls=[tool_call.model_dump() for tool_call in result.tool_calls],
            metadata={"provider": agent.provider.provider, "model": agent.provider.model},
            session_id=session_id,
        )

    @app.post("/agents/{agent_name}/stream")
    async def stream_agent(request: Request, agent_name: str, run_request: AgentRunRequest) -> StreamingResponse:
        db_manager: DatabaseManager = app.state.db_manager
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime
        session_store: SessionStore = app.state.session_store
        principal = await _resolve_console_principal(request, db_manager)
        resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
        await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
        session_id = run_request.session_id or _new_chat_item_id("session")
        # Record sandbox metadata before the container is created.
        try:
            agent = registry.get_agent(resolved_agent_name)
            _record_sandbox_session(getattr(app.state, "execution_backend", None), session_id, agent)
        except Exception:
            logger.debug("Failed to record sandbox session metadata for %s", session_id, exc_info=True)
        try:
            agent = registry.get_agent(resolved_agent_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

        existing = await session_store.get_session(session_id)
        if existing is not None:
            _ensure_console_principal_can_access_session(principal, existing)
        pending_input = _extract_pending_user_input(existing.activity) if existing else None
        resume_tool_result = _build_resume_tool_result(run_request, pending_input)
        if pending_input is not None and resume_tool_result is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Session '{session_id}' is waiting for an answer to '{pending_input.title}'. "
                    "Submit the pending question response before sending a new message."
                ),
            )

        async def event_stream():
            transcript_messages = [message.model_copy(deep=True) for message in existing.messages] if existing else []
            activity = [item.model_copy(deep=True) for item in existing.activity] if existing else []
            user_transcript = _build_user_transcript_message(run_request)
            transcript_messages.append(user_transcript)
            assistant_message_id = _new_chat_item_id("assistant")
            runtime_metadata = dict(run_request.metadata or {})
            if resume_tool_result is not None:
                runtime_metadata["resume_tool_result"] = resume_tool_result.model_dump(mode="json")
                activity.append(
                    ChatActivityItem(
                        id=_new_chat_item_id(SSE_EVENT_INPUT_RESOLVED),
                        title=SSE_EVENT_INPUT_RESOLVED,
                        payload={
                            "id": resume_tool_result.request_id,
                            "summary": resume_tool_result.summary,
                            "answers": resume_tool_result.answers,
                        },
                    )
                )

            try:
                async for event in runtime.stream_events(
                    agent,
                    run_request.input,
                    RunContext(agent_name=resolved_agent_name, session_id=session_id, metadata=runtime_metadata, execution_backend=getattr(app.state, "execution_backend", None)),
                ):
                    event_name = event["event"]
                    payload = event["payload"]
                    if event_name == SSE_EVENT_ASSISTANT:
                        text = _payload_text(payload)
                        if text:
                            _upsert_assistant_transcript(transcript_messages, assistant_message_id, text)
                    elif event_name == SSE_EVENT_FINAL:
                        text = _payload_output_text(payload)
                        if text:
                            _replace_assistant_transcript(transcript_messages, assistant_message_id, text)
                    elif event_name in TRACE_ACTIVITY_EVENTS:
                        activity.append(ChatActivityItem(id=_new_chat_item_id(event_name), title=event_name, payload=payload))
                        if event_name in {SSE_EVENT_TOOL_RESULTS, SSE_EVENT_DELEGATE_TOOL_RESULTS}:
                            _append_assistant_attachments(
                                transcript_messages,
                                assistant_message_id,
                                _published_download_attachments_from_tool_results(payload),
                            )

                    yield runtime._encode_sse(event_name, payload)
            except ModelProviderError as exc:
                status_code = 502 if exc.status_code is None else min(max(exc.status_code, 400), 599)
                payload = {"status_code": status_code, "detail": exc.detail}
                activity.append(ChatActivityItem(id=_new_chat_item_id(SSE_EVENT_ERROR), title=SSE_EVENT_ERROR, payload=payload))
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("Agent stream failed", extra={"agent_name": agent_name, "session_id": session_id})
                payload = {"status_code": 500, "detail": str(exc) or "Agent stream failed unexpectedly."}
                activity.append(ChatActivityItem(id=_new_chat_item_id(SSE_EVENT_ERROR), title=SSE_EVENT_ERROR, payload=payload))
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                memory_messages = await session_store.load_messages(session_id)
                now = datetime.now(UTC)
                title_source = existing.title_source if existing else "auto"
                title = existing.title if existing else "New conversation"
                if title_source == "manual":
                    resolved_title = title
                elif existing and existing.message_count > 0 and title and title != "New conversation":
                    resolved_title = title
                else:
                    resolved_title = await _generate_session_title(registry, agent, transcript_messages)

                saved = await session_store.save_session(
                    ChatSessionRecord(
                        id=session_id,
                        title=resolved_title,
                        title_source=title_source,
                        agent_name=agent.name,
                        owner_user_id=principal.user_id,
                        workspace_id=principal.workspace_id,
                        preview_text=_build_session_preview(transcript_messages),
                        created_at=existing.created_at if existing else now,
                        updated_at=existing.updated_at if existing else now,
                        memory_messages=memory_messages,
                        messages=transcript_messages,
                        activity=activity,
                    )
                )
                yield runtime._encode_sse(
                    SSE_EVENT_SESSION,
                    {
                        "id": saved.id,
                        "title": saved.title,
                        "title_source": saved.title_source,
                        "agent_name": saved.agent_name,
                        "preview_text": saved.preview_text,
                        "message_count": saved.message_count,
                        "created_at": saved.created_at.isoformat(),
                        "updated_at": saved.updated_at.isoformat(),
                    },
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/config/{kind}")
    async def get_config(request: Request, kind: str) -> ConfigDocumentResponse:
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized = _normalize_config_kind(kind)
        payload = await config_store.get_document(normalized, principal.config)
        return _config_document_response(normalized, payload, settings)

    @app.put("/config/{kind}")
    async def put_config(request: Request, kind: str, update_request: ConfigDocumentUpdateRequest) -> ConfigDocumentResponse:
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized = _normalize_config_kind(kind)
        try:
            raw_payload = json.loads(update_request.raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

        if not isinstance(raw_payload, list):
            raise HTTPException(status_code=400, detail="Config payload must be a JSON array")

        try:
            validated = _validate_config_payload(normalized, raw_payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors()) from exc
        agent_renames = _extract_agent_renames(update_request.metadata) if normalized == "agents" else None
        payload = await config_store.save_document(normalized, validated, principal=principal.config, agent_renames=agent_renames)
        global_payload = await config_store.get_document(normalized)
        await _apply_runtime_config(app, normalized, global_payload)
        return _config_document_response(normalized, payload, settings)

    @app.post("/config/{kind}/{resource_name}/publish-request")
    async def request_config_publication(request: Request, kind: str, resource_name: str) -> PublicationRequestResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized = _normalize_config_kind(kind)
        return await _request_resource_publication(db_manager, principal, normalized, resource_name, request)

    @app.post("/config/{kind}/{resource_name}/publication-review")
    async def review_config_publication(
        request: Request,
        kind: str,
        resource_name: str,
        review: PublicationReviewRequest,
    ) -> PublicationRequestResponse:
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized = _normalize_config_kind(kind)
        response = await _review_resource_publication(db_manager, principal, normalized, resource_name, review.status, request)
        global_payload = await config_store.get_document(normalized)
        await _apply_runtime_config(app, normalized, global_payload)
        return response

    @app.get("/management/{kind}/export")
    async def export_management_config(request: Request, kind: str, format: str = "yaml") -> ManagementExportResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized_kind = _normalize_management_kind(kind)
        normalized_format = _normalize_management_export_format(format)
        payload, item_count = await _build_management_export_payload(app, normalized_kind, principal)
        content = _serialize_management_export_payload(payload, normalized_format)
        extension = "yaml" if normalized_format == "yaml" else "json"
        content_type = "application/x-yaml" if normalized_format == "yaml" else "application/json"
        return ManagementExportResponse(
            kind=normalized_kind,
            format=normalized_format,
            file_name=f"agent-framework-{normalized_kind}.{extension}",
            content_type=content_type,
            content=content,
            item_count=item_count,
        )

    @app.post("/management/{kind}/import")
    async def import_management_config(request: Request, kind: str, file: UploadFile = File(...)) -> ManagementImportResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        normalized_kind = _normalize_management_kind(kind)
        raw = await file.read()
        if len(raw) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded file exceeds maximum size ({settings.max_upload_bytes} bytes)",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Imported file must be UTF-8 encoded text") from exc
        return await _import_management_payload(app, normalized_kind, text, file.filename, principal, request)

    @app.get("/skills")
    async def list_skills(request: Request) -> list[SkillSummaryResponse]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        visible_sources = await config_store.get_document("skill_sources", principal.config)
        results: list[SkillSummaryResponse] = []
        for name, spec in registry.manifest_skills.items():
            source_payload = _visible_skill_source_payload_for_spec(spec, visible_sources)
            if not _can_access_manifest_skill(spec, settings, source_payload):
                continue
            results.append(_manifest_skill_summary_response(registry, settings, spec, source_payload))
        for name, spec in registry.skills.items():
            if name not in registry.manifest_skills:
                results.append(_inline_skill_summary_response(registry, spec))
        return results

    @app.get("/skills/{skill_name}")
    async def get_skill(request: Request, skill_name: str) -> SkillSummaryResponse:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        manifest = registry.manifest_skills.get(skill_name)
        if manifest:
            source_payload = _visible_skill_source_payload_for_spec(manifest, await config_store.get_document("skill_sources", principal.config))
            if not _can_access_manifest_skill(manifest, settings, source_payload):
                raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
            return _manifest_skill_summary_response(registry, settings, manifest, source_payload)
        spec = registry.skills.get(skill_name)
        if spec:
            return _inline_skill_summary_response(registry, spec)
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    @app.get("/skills/{skill_name}/preview")
    async def preview_skill(request: Request, skill_name: str) -> SkillPreviewResponse:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        manifest = registry.manifest_skills.get(skill_name)
        if manifest:
            source_payload = _visible_skill_source_payload_for_spec(manifest, await config_store.get_document("skill_sources", principal.config))
            if not _can_access_manifest_skill(manifest, settings, source_payload):
                raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
            preview_root, preview_path_base = _resolve_skill_preview_paths(manifest, settings)
            files = _collect_skill_preview_files(preview_root, preview_path_base) if preview_root is not None else []
            return SkillPreviewResponse(name=manifest.name, source_dir=manifest.source_dir, files=files)

        spec = registry.skills.get(skill_name)
        if spec:
            return SkillPreviewResponse(
                name=spec.name,
                files=[
                    SkillPreviewFileResponse(
                        path="instructions.md",
                        language="markdown",
                        content=spec.instructions,
                    )
                ],
            )

        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    @app.post("/skills/install")
    async def install_skill(request: Request, install_request: SkillInstallRequest) -> SkillInstallResponse:
        registry: FrameworkRegistry = app.state.registry
        loader: SkillLoader = app.state.skill_loader
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        source_type = install_request.source_type or _infer_skill_install_source_type(settings, install_request.source)

        if source_type == "directory":
            source_dir = loader.settings.resolve_path(install_request.source)
            if source_dir is None or not source_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Source path does not exist or is not a directory: {install_request.source}")

            from agent_framework.skills.exceptions import SkillLoadError

            try:
                spec = loader.load_skill_dir(source_dir)
            except SkillLoadError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if install_request.category == "github_synced":
                raise HTTPException(status_code=400, detail="Directory installs must use built_in, uploaded, or authored categories")

            target_dir = settings.managed_skill_directory(install_request.category) / spec.name
            if target_dir.exists():
                status = "already_exists"
            else:
                # Skill source dirs can be large; copy off the event loop.
                await anyio.to_thread.run_sync(
                    functools.partial(shutil.copytree, source_dir, target_dir)
                )
                status = "installed"
            registered_spec = loader.load_skill_dir(target_dir)
            registry.register_manifest_skill(registered_spec)
            await _sync_registry_skill_states(registry, config_store)
            await _reconcile_skill_process_manager(registry, app.state.execution_backend)
            return SkillInstallResponse(
                name=registered_spec.name,
                version=registered_spec.version,
                description=registered_spec.description,
                status=status,
            )

        normalized_source = normalize_git_source_payload(
            {
                "source_type": "git",
                "category": "github_synced",
                "name": install_request.name,
                "url": install_request.source,
                "ref": install_request.ref,
                "subdir": install_request.subdir,
            }
        )
        if normalized_source is None:
            raise HTTPException(status_code=400, detail="Invalid git skill source")

        existing_sources = await config_store.get_document("skill_sources", principal.config)
        normalized_payload = PersistedSkillSourceConfig.model_validate(normalized_source).model_dump(mode="json")
        already_exists = any(
            PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json") == normalized_payload
            for item in existing_sources
        )
        if not already_exists:
            existing_sources.append(normalized_payload)
            existing_sources = await config_store.save_document("skill_sources", existing_sources, principal=principal.config)
        global_sources = await config_store.get_document("skill_sources")
        await _apply_runtime_config(app, "skill_sources", global_sources)

        installed_spec = _find_matching_git_skill(registry, normalized_payload)
        if installed_spec is None:
            raise HTTPException(status_code=500, detail="Git skill source synced, but no loadable skill was discovered")
        if not normalized_payload.get("name"):
            normalized_payload["name"] = installed_spec.name
            patched_sources = [
                normalized_payload if PersistedSkillSourceConfig.model_validate(item).url == normalized_payload.get("url") else item
                for item in existing_sources
            ]
            await config_store.save_document("skill_sources", patched_sources, principal=principal.config)
            await _apply_runtime_config(app, "skill_sources", await config_store.get_document("skill_sources"))

        return SkillInstallResponse(
            name=installed_spec.name,
            version=installed_spec.version,
            description=installed_spec.description,
            status="already_exists" if already_exists else "installed",
        )

    @app.post("/skills/upload")
    async def upload_skill(
        request: Request,
        file: UploadFile = File(...),
        category: str = Form("uploaded"),
    ) -> SkillInstallResponse:
        settings: AppSettings = app.state.settings
        if category not in {"uploaded", "authored"}:
            raise HTTPException(status_code=400, detail="Uploaded skills must use the uploaded or authored category")
        if not file.filename or not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Only .zip skill bundles are currently supported")

        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            archive_path = temp_dir / file.filename
            raw_content = await file.read()
            if len(raw_content) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Uploaded file exceeds maximum size ({settings.max_upload_bytes} bytes)",
                )
            archive_path.write_bytes(raw_content)
            extracted_root = temp_dir / "extracted"
            extracted_root.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    _safe_extract_zip(archive, extracted_root)
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=400, detail="Uploaded file is not a valid zip archive") from exc

            source_dir = _detect_uploaded_skill_directory(extracted_root)
            if source_dir is None:
                raise HTTPException(status_code=400, detail="Uploaded archive must contain exactly one skill bundle")

            return await install_skill(
                request,
                SkillInstallRequest(
                    source=str(source_dir),
                    source_type="directory",
                    category=category,
                )
            )

    @app.delete("/skills/{skill_name}")
    async def uninstall_skill(request: Request, skill_name: str) -> dict[str, str]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        if skill_name not in registry.manifest_skills and skill_name not in registry.skills:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
        await _ensure_skill_access(app, skill_name, principal)

        if registry.skill_process_manager:
            await registry.skill_process_manager.stop_skill(skill_name)

        spec = registry.manifest_skills.get(skill_name)
        if spec and _skill_category(spec, settings) == "built_in":
            raise HTTPException(status_code=400, detail="Built-in skills cannot be uninstalled through the API")

        spec = registry.unregister_skill(skill_name)
        await config_store.delete_skill_state(skill_name)
        await _reconcile_skill_process_manager(registry, app.state.execution_backend)

        if spec:
            category = _skill_category(spec, settings)
            source_dir = Path(spec.source_dir) if spec.source_dir else None
            if spec.source_type == "git":
                existing_sources = await config_store.get_document("skill_sources", principal.config)
                remaining_sources = [
                    item
                    for item in existing_sources
                    if not _matches_skill_source(spec, PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json"))
                ]
                if len(remaining_sources) != len(existing_sources):
                    await config_store.save_document("skill_sources", remaining_sources, principal=principal.config)
                    await _apply_runtime_config(app, "skill_sources", await config_store.get_document("skill_sources"))
                still_referenced = any(
                    _matches_skill_source(spec, PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json"))
                    for item in await config_store.get_document("skill_sources")
                )
                if source_dir and source_dir.exists() and not still_referenced:
                    repo_root = settings.managed_skill_directory("github_synced")
                    for candidate in [source_dir, *source_dir.parents]:
                        if candidate.parent == repo_root:
                            await _rmtree_async(candidate, ignore_errors=True)
                            break
            elif category in {"uploaded", "authored"} and source_dir and source_dir.exists():
                await _rmtree_async(source_dir, ignore_errors=True)

        return {"status": "uninstalled", "skill": skill_name}

    @app.get("/skills/{skill_name}/export")
    async def export_skill(request: Request, skill_name: str) -> FileResponse:
        registry: FrameworkRegistry = app.state.registry
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        manifest = registry.manifest_skills.get(skill_name)
        if not manifest:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
        if not manifest.source_dir:
            raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' has no local source directory")

        source = Path(manifest.source_dir)
        if not source.is_dir():
            raise HTTPException(status_code=404, detail=f"Skill directory not found: {source}")

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, prefix=f"{skill_name}_")
        try:
            await anyio.to_thread.run_sync(_build_skill_export_zip, source, tmp.name)
        except Exception:
            os.unlink(tmp.name)
            raise

        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename=f"{skill_name}.zip",
            # Unlink the on-disk zip after the response finishes streaming so
            # repeated exports don't accumulate temp files.
            background=BackgroundTask(os.unlink, tmp.name),
        )

    @app.post("/skills/{skill_name}/enable")
    async def enable_skill(request: Request, skill_name: str) -> dict[str, str]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        await _ensure_skill_state_mutation_allowed(app, skill_name, principal)
        await _set_skill_enabled(app, skill_name, True)
        return {"status": "enabled", "skill": skill_name}

    @app.post("/skills/{skill_name}/disable")
    async def disable_skill(request: Request, skill_name: str) -> dict[str, str]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        await _ensure_skill_state_mutation_allowed(app, skill_name, principal)
        await _set_skill_enabled(app, skill_name, False)
        return {"status": "disabled", "skill": skill_name}

    @app.post("/skills/{skill_name}/start")
    async def start_skill(request: Request, skill_name: str) -> dict[str, Any]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        registry: FrameworkRegistry = app.state.registry
        manifest = registry.manifest_skills.get(skill_name)
        if not manifest:
            raise HTTPException(status_code=404, detail=f"Unknown manifest skill: {skill_name}")
        if not registry.is_skill_enabled(skill_name):
            raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' is disabled")
        if not manifest.is_executable:
            return {"status": "not_executable", "skill": skill_name}
        if not registry.skill_process_manager:
            raise HTTPException(status_code=400, detail="Skill process manager is not initialized")

        handle = await registry.skill_process_manager.acquire(manifest)
        await registry.skill_process_manager.release(handle)
        return {"status": "started", "skill": skill_name}

    @app.post("/skills/{skill_name}/stop")
    async def stop_skill(request: Request, skill_name: str) -> dict[str, str]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        registry: FrameworkRegistry = app.state.registry
        if not registry.skill_process_manager:
            return {"status": "stopped", "skill": skill_name}

        await registry.skill_process_manager.stop_skill(skill_name)
        return {"status": "stopped", "skill": skill_name}

    @app.get("/skills/{skill_name}/health")
    async def skill_health(request: Request, skill_name: str) -> dict[str, Any]:
        principal = await _resolve_console_principal(request, app.state.db_manager)
        await _ensure_skill_access(app, skill_name, principal)
        registry: FrameworkRegistry = app.state.registry
        manifest = registry.manifest_skills.get(skill_name)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"Unknown manifest skill: {skill_name}")
        if not registry.is_skill_enabled(skill_name):
            return {"status": "disabled", "pools": {}}
        if not manifest.is_executable:
            return {"status": "not_executable", "pools": {}}
        if not registry.skill_process_manager:
            return {"status": "not_loaded", "pools": {}}

        return {"status": "running", "pools": registry.skill_process_manager.pool_status(skill_name)}

    @app.post("/mcp/inspect")
    async def inspect_mcp_server(request: Request, inspect_request: McpInspectRequest) -> McpInspectResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        await _ensure_console_principal_can_access_mcp_server(db_manager, principal, inspect_request.server.name)
        client = McpSdkClient()
        try:
            tools = await client.list_tools(inspect_request.server)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return McpInspectResponse(
            server=inspect_request.server,
            tools=[
                McpToolSummaryResponse(
                    name=tool.tool_name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in tools
            ],
        )

    @app.post("/mcp/call")
    async def call_mcp_tool(request: Request, call_request: McpToolCallRequest) -> McpToolCallResponse:
        db_manager: DatabaseManager = app.state.db_manager
        principal = await _resolve_console_principal(request, db_manager)
        await _ensure_console_principal_can_access_mcp_server(db_manager, principal, call_request.server.name)
        client = McpSdkClient()
        try:
            result = await client.call_tool(call_request.server, call_request.tool_name, call_request.arguments)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return McpToolCallResponse(name=result.name, content=result.content, is_error=result.is_error)

    return app

