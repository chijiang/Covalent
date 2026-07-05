from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import logging
import os
import mimetypes
from pathlib import Path
import re
import shutil
import tempfile
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4
import zipfile

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError
import yaml

from agent_framework.api.schemas import (
    AgentRunRequest,
    AgentRunResponse,
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenSummaryResponse,
    LocalToolSummaryResponse,
    AgentSummaryResponse,
    AttachmentUploadResponse,
    AttachmentUploadItemResponse,
    ChatSessionActivityResponse,
    ChatSessionMessageResponse,
    ChatSessionResponse,
    ChatSessionSummaryResponse,
    ChatSessionUpdateRequest,
    ConfigDocumentResponse,
    ConfigDocumentUpdateRequest,
    ManagementExportFormat,
    ManagementExportResponse,
    ManagementImportResponse,
    ManagementKind,
    McpInspectRequest,
    McpInspectResponse,
    McpToolCallRequest,
    McpToolCallResponse,
    McpToolSummaryResponse,
    ProviderSummaryResponse,
    PublicAgentInvokeRequest,
    PublicAgentInvokeResponse,
    SkillInstallRequest,
    SkillInstallResponse,
    SkillManagementItemResponse,
    SkillManagementSourceResponse,
    SkillPreviewFileResponse,
    SkillPreviewResponse,
    SkillSummaryResponse,
)
from agent_framework.api.auth import (
    ApiPrincipal,
    authenticate_api_token,
    generate_api_token,
    hash_api_token,
    require_agent_allowed,
    require_memory_mode_allowed,
    require_scope,
    require_trace_level_allowed,
)
from agent_framework.core.attachment_processing import process_attachment_bytes
from agent_framework.core.agent import AgentSpec
from agent_framework.core.workspace_tools import register_workspace_tools
from agent_framework.core.types import Capability, GenerationRequest, Message, ResumedToolResult, RunContext, UserInputRequest, UserQuestion, UserQuestionOption
from agent_framework.infra.config_store import ConfigKind, ConfigStore, PersistedAgentConfig, PersistedSkillSourceConfig
from sqlalchemy import select, text

from agent_framework.infra.db import (
    AgentRunLogRow,
    ApiTokenRow,
    ChatSessionRow,
    DatabaseManager,
    UserRow,
    WorkspaceMemberRow,
    WorkspaceRow,
)
from agent_framework.infra.migrations import run_database_migrations
from agent_framework.infra.memory import (
    ChatActivityItem,
    ChatSessionRecord,
    ChatSessionSummary,
    ChatTranscriptMessage,
    PersistentSessionStore,
    SessionStore,
)
from agent_framework.infra.settings import AppSettings
from agent_framework.mcp.client import McpSdkClient
from agent_framework.mcp.spec import McpServerConfig, McpToolReference
from agent_framework.model.base import ModelProviderError, ProviderConfig
from agent_framework.model.factory import default_provider_config
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.react import ReactAgentRuntime
from agent_framework.skills.loader import SkillLoader, normalize_git_source_payload
from agent_framework.skills.meta_tools import register_skill_meta_tools
from agent_framework.skills.process import SkillProcessManager
from agent_framework.skills.spec import ManifestSkillSpec

logger = logging.getLogger(__name__)

LEGACY_REASONING_SKILL_NAME = "general_reasoning"

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

WORKSPACE_AGENT_TOOLS = (
    "list_workspace_files",
    "read_workspace_file",
    "write_workspace_file",
    "create_workspace_directory",
    "delete_workspace_entry",
    "publish_downloadable_file",
)
BUILTIN_AGENT_TOOLS = ("get_current_time", "ask_user", *WORKSPACE_AGENT_TOOLS)
DEFAULT_AGENT_LOCAL_TOOLS = ("get_current_time",)
_SAFE_STORAGE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SKILL_PREVIEW_IGNORED_DIRS = {".git", ".next", ".venv", "__pycache__", "node_modules", "venv"}
_SKILL_PREVIEW_MAX_BYTES = 128 * 1024
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


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    for info in archive.infolist():
        if info.is_dir():
            continue
        extracted = (target_dir / info.filename).resolve()
        if not extracted.is_relative_to(target_dir):
            raise ValueError(f"Zip entry escapes target directory: {info.filename}")
    archive.extractall(target_dir)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def _default_agent_local_tools(settings: AppSettings | None) -> list[str]:
    if settings is not None and not settings.enable_builtin_tools:
        return []
    return list(DEFAULT_AGENT_LOCAL_TOOLS)


def _normalize_agent_payload_item(item: dict[str, object], settings: AppSettings | None) -> dict[str, object]:
    normalized = dict(item)
    legacy_reasoning_skill_name = settings.reasoning_skill_name if settings is not None else LEGACY_REASONING_SKILL_NAME
    skills = _dedupe_strings([str(value) for value in normalized.get("skills", []) if isinstance(value, str)])
    local_tools = [t for t in _dedupe_strings([str(value) for value in normalized.get("local_tools", []) if isinstance(value, str)]) if t != "echo"]
    reasoning_prompt_raw = normalized.get("reasoning_prompt")
    reasoning_prompt = reasoning_prompt_raw.strip() if isinstance(reasoning_prompt_raw, str) else ""
    reasoning_level_raw = normalized.get("reasoning_level")
    reasoning_level = reasoning_level_raw.strip().lower() if isinstance(reasoning_level_raw, str) else "none"
    if not reasoning_level:
        reasoning_level = "none"

    if legacy_reasoning_skill_name in skills:
        skills = [skill for skill in skills if skill != legacy_reasoning_skill_name]
        if not reasoning_prompt and settings is not None:
            reasoning_prompt = settings.reasoning_skill_instructions

    normalized["skills"] = skills
    if "local_tools" not in item:
        local_tools = _dedupe_strings(local_tools + _default_agent_local_tools(settings))
    normalized["local_tools"] = local_tools
    normalized["reasoning_prompt"] = reasoning_prompt
    normalized["reasoning_level"] = reasoning_level
    return normalized


async def build_registry(
    settings: AppSettings,
    config_store: ConfigStore,
) -> tuple[FrameworkRegistry, SkillLoader, list[dict[str, object]]]:
    settings.ensure_managed_skill_directories()
    registry = FrameworkRegistry()
    register_skill_meta_tools(registry, settings)
    mcp_payload = await config_store.ensure_document("mcp", _seed_mcp_payload(settings))
    mcp_servers = _parse_mcp_servers(mcp_payload)
    if settings.enable_builtin_tools:
        register_builtin_tools(registry, settings)

    if settings.mcp_enabled:
        registry.set_mcp_client(McpSdkClient())
        for server in mcp_servers:
            registry.register_mcp_server(server)

    provider_config = await _resolve_default_provider(settings, config_store)
    agent_payload = await config_store.ensure_document(
        "agents",
        _seed_agent_payload(settings, provider_config, mcp_servers),
    )
    for agent in _build_agent_specs(agent_payload, provider_config, mcp_servers, settings):
        registry.register_agent(agent)

    loader = SkillLoader(settings)
    skill_source_payload = await config_store.ensure_document("skill_sources", _seed_skill_source_payload(settings))
    manifest_skills = loader.discover_local()
    for spec in manifest_skills:
        registry.register_manifest_skill(spec)
        logger.info("Loaded manifest skill '%s' (v%s) from %s", spec.name, spec.version, spec.source_dir)

    return registry, loader, skill_source_payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppSettings()
    database_url = settings.database_url
    if not database_url:
        raise RuntimeError("AGENT_FRAMEWORK_DATABASE_URL must be set when using persistent config storage")
    await anyio.to_thread.run_sync(run_database_migrations, database_url.replace('+asyncpg', ''))
    db_manager = DatabaseManager(database_url)
    config_store = ConfigStore(db_manager.session_factory)
    registry, loader, skill_source_payload = await build_registry(settings, config_store)

    git_skills = await loader.discover_git(skill_source_payload)
    for spec in git_skills:
        registry.register_manifest_skill(spec)
        logger.info("Loaded git skill '%s' (v%s) from %s", spec.name, spec.version, spec.source_dir)

    await _sync_registry_skill_states(registry, config_store)
    await _reconcile_skill_process_manager(registry)

    app.state.settings = settings
    app.state.db_manager = db_manager
    app.state.config_store = config_store
    app.state.registry = registry
    app.state.skill_loader = loader
    app.state.session_store = PersistentSessionStore(db_manager.session_factory)
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
    await registry.aclose()
    await db_manager.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Covalent", version="0.3.0", lifespan=lifespan)
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

        return checks

    @app.get("/api-tokens")
    async def list_api_tokens() -> list[ApiTokenSummaryResponse]:
        db_manager: DatabaseManager = app.state.db_manager
        return await _list_api_token_summaries(db_manager)

    @app.post("/api-tokens")
    async def create_api_token(request: ApiTokenCreateRequest) -> ApiTokenCreateResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        return await _create_api_token(db_manager, settings, request)

    @app.delete("/api-tokens/{token_id}")
    async def revoke_api_token(token_id: str) -> ApiTokenSummaryResponse:
        db_manager: DatabaseManager = app.state.db_manager
        return await _revoke_api_token(db_manager, token_id)

    @app.post("/v1/agent/invoke", response_model=None)
    async def public_invoke_agent(request: Request, invoke_request: PublicAgentInvokeRequest) -> PublicAgentInvokeResponse | StreamingResponse:
        settings: AppSettings = app.state.settings
        db_manager: DatabaseManager = app.state.db_manager
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime

        principal = await authenticate_api_token(
            request,
            settings=settings,
            session_factory=db_manager.session_factory,
        )
        memory_mode = invoke_request.memory.mode
        trace_level = invoke_request.trace.level
        require_scope(principal, "agent:invoke")
        require_agent_allowed(principal, invoke_request.agent)
        require_memory_mode_allowed(principal, memory_mode)
        require_trace_level_allowed(principal, trace_level)

        try:
            agent = registry.get_agent(invoke_request.agent)
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
        )

        if invoke_request.stream:
            async def event_stream():
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

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

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
    async def list_provider_models(provider_name: str) -> list[str]:
        """Fetch available models from an OpenAI-compatible provider."""
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        providers = await config_store.get_document("providers")
        target = None
        for p in providers:
            if p.get("name") == provider_name:
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
    async def list_sessions() -> list[ChatSessionSummaryResponse]:
        session_store: SessionStore = app.state.session_store
        return [to_chat_session_summary_response(record) for record in await session_store.list_sessions()]

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> ChatSessionResponse:
        session_store: SessionStore = app.state.session_store
        record = await session_store.get_session(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        return to_chat_session_response(record)

    @app.patch("/sessions/{session_id}")
    async def rename_session(session_id: str, request: ChatSessionUpdateRequest) -> ChatSessionResponse:
        session_store: SessionStore = app.state.session_store
        try:
            record = await session_store.update_title(session_id, request.title.strip(), "manual")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        return to_chat_session_response(record)

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        settings: AppSettings = app.state.settings
        session_store: SessionStore = app.state.session_store
        if not await session_store.delete_session(session_id):
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        shutil.rmtree(_attachment_session_dir(settings.workspace_root(), session_id), ignore_errors=True)
        shutil.rmtree(_chat_upload_session_dir(settings, session_id), ignore_errors=True)
        shutil.rmtree(_download_session_dir(settings.workspace_root(), session_id), ignore_errors=True)
        if settings.session_workspace_enabled:
            shutil.rmtree(settings.session_workspace_dir(session_id), ignore_errors=True)
        return {"status": "deleted", "id": session_id}

    @app.post("/attachments/upload")
    async def upload_attachments(
        session_id: str = Form(...),
        delivery_mode: Literal["parse", "workspace"] = Form("parse"),
        metadata_json: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ) -> AttachmentUploadResponse:
        settings: AppSettings = app.state.settings
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
            content_type = str(file.content_type or metadata.get("type") or "application/octet-xx")
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
    async def download_published_file(session_id: str, file_name: str) -> FileResponse:
        settings: AppSettings = app.state.settings
        normalized_name = Path(file_name).name
        if not normalized_name or normalized_name != file_name:
            raise HTTPException(status_code=404, detail="Unknown download")

        target_path = _download_session_dir(settings.workspace_root(), session_id) / normalized_name
        if not target_path.is_file():
            raise HTTPException(status_code=404, detail="Unknown download")

        media_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
        return FileResponse(target_path, media_type=media_type, filename=target_path.name)

    @app.get("/agents")
    async def list_agents() -> list[dict[str, str]]:
        registry: FrameworkRegistry = app.state.registry
        return [{"name": agent.name, "description": agent.description} for agent in registry.agents.values()]

    @app.get("/local-tools")
    async def list_local_tools() -> list[LocalToolSummaryResponse]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        return _available_local_tool_summaries(registry, settings)

    @app.get("/agents/{agent_name}")
    async def get_agent(agent_name: str) -> AgentSummaryResponse:
        registry: FrameworkRegistry = app.state.registry
        try:
            return to_agent_summary(registry.get_agent(agent_name))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

    @app.post("/agents/{agent_name}/run")
    async def run_agent(agent_name: str, request: AgentRunRequest) -> AgentRunResponse:
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime
        session_id = request.session_id or _new_chat_item_id("session")
        try:
            agent = registry.get_agent(agent_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

        try:
            result = await runtime.run(
                agent,
                request.input,
                RunContext(agent_name=agent_name, session_id=session_id, metadata=request.metadata),
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
    async def stream_agent(agent_name: str, request: AgentRunRequest) -> StreamingResponse:
        registry: FrameworkRegistry = app.state.registry
        runtime: ReactAgentRuntime = app.state.runtime
        session_store: SessionStore = app.state.session_store
        session_id = request.session_id or _new_chat_item_id("session")
        try:
            agent = registry.get_agent(agent_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

        existing = await session_store.get_session(session_id)
        pending_input = _extract_pending_user_input(existing.activity) if existing else None
        resume_tool_result = _build_resume_tool_result(request, pending_input)
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
            user_transcript = _build_user_transcript_message(request)
            transcript_messages.append(user_transcript)
            assistant_message_id = _new_chat_item_id("assistant")
            runtime_metadata = dict(request.metadata or {})
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
                    request.input,
                    RunContext(agent_name=agent_name, session_id=session_id, metadata=runtime_metadata),
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
    async def get_config(kind: str) -> ConfigDocumentResponse:
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        normalized = _normalize_config_kind(kind)
        payload = await config_store.get_document(normalized)
        return _config_document_response(normalized, payload, settings)

    @app.put("/config/{kind}")
    async def put_config(kind: str, request: ConfigDocumentUpdateRequest) -> ConfigDocumentResponse:
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        normalized = _normalize_config_kind(kind)
        try:
            raw_payload = json.loads(request.raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

        if not isinstance(raw_payload, list):
            raise HTTPException(status_code=400, detail="Config payload must be a JSON array")

        try:
            validated = _validate_config_payload(normalized, raw_payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors()) from exc
        agent_renames = _extract_agent_renames(request.metadata) if normalized == "agents" else None
        payload = await config_store.save_document(normalized, validated, agent_renames=agent_renames)
        await _apply_runtime_config(app, normalized, payload)
        return _config_document_response(normalized, payload, settings)

    @app.get("/management/{kind}/export")
    async def export_management_config(kind: str, format: str = "yaml") -> ManagementExportResponse:
        normalized_kind = _normalize_management_kind(kind)
        normalized_format = _normalize_management_export_format(format)
        payload, item_count = await _build_management_export_payload(app, normalized_kind)
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
    async def import_management_config(kind: str, file: UploadFile = File(...)) -> ManagementImportResponse:
        settings: AppSettings = app.state.settings
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
        return await _import_management_payload(app, normalized_kind, text, file.filename)

    @app.get("/skills")
    async def list_skills() -> list[SkillSummaryResponse]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        results: list[SkillSummaryResponse] = []
        for name, spec in registry.manifest_skills.items():
            results.append(
                SkillSummaryResponse(
                    name=spec.name,
                    version=spec.version,
                    description=spec.description,
                    source_type=spec.source_type,
                    category=_skill_category(spec, settings),
                    source_dir=spec.source_dir,
                    runtime_type=spec.runtime.type if spec.runtime else None,
                    tools=[t.name for t in spec.tools],
                    references=spec.references,
                    enabled=registry.is_skill_enabled(name),
                )
            )
        for name, spec in registry.skills.items():
            if name not in registry.manifest_skills:
                results.append(
                    SkillSummaryResponse(
                        name=spec.name,
                        version=spec.metadata.get("version", "0.0.0"),
                        description=spec.description,
                        source_type="local",
                        runtime_type="python",
                        tools=spec.tools,
                        references=[],
                        enabled=registry.is_skill_enabled(name),
                    )
                )
        return results

    @app.get("/skills/{skill_name}")
    async def get_skill(skill_name: str) -> SkillSummaryResponse:
        registry: FrameworkRegistry = app.state.registry
        manifest = registry.manifest_skills.get(skill_name)
        if manifest:
            return SkillSummaryResponse(
                name=manifest.name,
                version=manifest.version,
                description=manifest.description,
                source_type=manifest.source_type,
                category=_skill_category(manifest, app.state.settings),
                source_dir=manifest.source_dir,
                runtime_type=manifest.runtime.type if manifest.runtime else None,
                tools=[t.name for t in manifest.tools],
                references=manifest.references,
                enabled=registry.is_skill_enabled(skill_name),
            )
        spec = registry.skills.get(skill_name)
        if spec:
            return SkillSummaryResponse(
                name=spec.name,
                version=spec.metadata.get("version", "0.0.0"),
                description=spec.description,
                source_type="local",
                runtime_type="python",
                tools=spec.tools,
                references=[],
                enabled=registry.is_skill_enabled(skill_name),
            )
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    @app.get("/skills/{skill_name}/preview")
    async def preview_skill(skill_name: str) -> SkillPreviewResponse:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        manifest = registry.manifest_skills.get(skill_name)
        if manifest:
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
    async def install_skill(request: SkillInstallRequest) -> SkillInstallResponse:
        registry: FrameworkRegistry = app.state.registry
        loader: SkillLoader = app.state.skill_loader
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        source_type = request.source_type or _infer_skill_install_source_type(settings, request.source)

        if source_type == "directory":
            source_dir = loader.settings.resolve_path(request.source)
            if source_dir is None or not source_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Source path does not exist or is not a directory: {request.source}")

            from agent_framework.skills.exceptions import SkillLoadError

            try:
                spec = loader.load_skill_dir(source_dir)
            except SkillLoadError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if request.category == "github_synced":
                raise HTTPException(status_code=400, detail="Directory installs must use built_in, uploaded, or authored categories")

            target_dir = settings.managed_skill_directory(request.category) / spec.name
            if target_dir.exists():
                status = "already_exists"
            else:
                shutil.copytree(source_dir, target_dir)
                status = "installed"
            registered_spec = loader.load_skill_dir(target_dir)
            registry.register_manifest_skill(registered_spec)
            await _sync_registry_skill_states(registry, config_store)
            await _reconcile_skill_process_manager(registry)
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
                "name": request.name,
                "url": request.source,
                "ref": request.ref,
                "subdir": request.subdir,
            }
        )
        if normalized_source is None:
            raise HTTPException(status_code=400, detail="Invalid git skill source")

        existing_sources = await config_store.get_document("skill_sources")
        normalized_payload = PersistedSkillSourceConfig.model_validate(normalized_source).model_dump(mode="json")
        already_exists = any(
            PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json") == normalized_payload
            for item in existing_sources
        )
        if not already_exists:
            existing_sources.append(normalized_payload)
            existing_sources = await config_store.save_document("skill_sources", existing_sources)
        await _apply_runtime_config(app, "skill_sources", existing_sources)

        installed_spec = _find_matching_git_skill(registry, normalized_payload)
        if installed_spec is None:
            raise HTTPException(status_code=500, detail="Git skill source synced, but no loadable skill was discovered")

        return SkillInstallResponse(
            name=installed_spec.name,
            version=installed_spec.version,
            description=installed_spec.description,
            status="already_exists" if already_exists else "installed",
        )

    @app.post("/skills/upload")
    async def upload_skill(
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
                SkillInstallRequest(
                    source=str(source_dir),
                    source_type="directory",
                    category=category,
                )
            )

    @app.delete("/skills/{skill_name}")
    async def uninstall_skill(skill_name: str) -> dict[str, str]:
        registry: FrameworkRegistry = app.state.registry
        settings: AppSettings = app.state.settings
        config_store: ConfigStore = app.state.config_store
        if skill_name not in registry.manifest_skills and skill_name not in registry.skills:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

        if registry.skill_process_manager:
            await registry.skill_process_manager.stop_skill(skill_name)

        spec = registry.manifest_skills.get(skill_name)
        if spec and _skill_category(spec, settings) == "built_in":
            raise HTTPException(status_code=400, detail="Built-in skills cannot be uninstalled through the API")

        spec = registry.unregister_skill(skill_name)
        await config_store.delete_skill_state(skill_name)
        await _reconcile_skill_process_manager(registry)

        if spec:
            category = _skill_category(spec, settings)
            source_dir = Path(spec.source_dir) if spec.source_dir else None
            if spec.source_type == "git":
                existing_sources = await config_store.get_document("skill_sources")
                remaining_sources = [
                    item
                    for item in existing_sources
                    if not _matches_skill_source(spec, PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json"))
                ]
                if len(remaining_sources) != len(existing_sources):
                    await config_store.save_document("skill_sources", remaining_sources)
                if source_dir and source_dir.exists():
                    repo_root = settings.managed_skill_directory("github_synced")
                    for candidate in [source_dir, *source_dir.parents]:
                        if candidate.parent == repo_root:
                            shutil.rmtree(candidate, ignore_errors=True)
                            break
            elif category in {"uploaded", "authored"} and source_dir and source_dir.exists():
                shutil.rmtree(source_dir, ignore_errors=True)

        return {"status": "uninstalled", "skill": skill_name}

    @app.get("/skills/{skill_name}/export")
    async def export_skill(skill_name: str) -> FileResponse:
        registry: FrameworkRegistry = app.state.registry
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
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for file_path in sorted(source.rglob("*")):
                    if not file_path.is_file():
                        continue
                    if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                        continue
                    archive.write(file_path, file_path.relative_to(source))
        except Exception:
            os.unlink(tmp.name)
            raise

        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename=f"{skill_name}.zip",
        )

    @app.post("/skills/{skill_name}/enable")
    async def enable_skill(skill_name: str) -> dict[str, str]:
        await _set_skill_enabled(app, skill_name, True)
        return {"status": "enabled", "skill": skill_name}

    @app.post("/skills/{skill_name}/disable")
    async def disable_skill(skill_name: str) -> dict[str, str]:
        await _set_skill_enabled(app, skill_name, False)
        return {"status": "disabled", "skill": skill_name}

    @app.post("/skills/{skill_name}/start")
    async def start_skill(skill_name: str) -> dict[str, Any]:
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
    async def stop_skill(skill_name: str) -> dict[str, str]:
        registry: FrameworkRegistry = app.state.registry
        if not registry.skill_process_manager:
            return {"status": "stopped", "skill": skill_name}

        await registry.skill_process_manager.stop_skill(skill_name)
        return {"status": "stopped", "skill": skill_name}

    @app.get("/skills/{skill_name}/health")
    async def skill_health(skill_name: str) -> dict[str, Any]:
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
    async def inspect_mcp_server(request: McpInspectRequest) -> McpInspectResponse:
        client = McpSdkClient()
        try:
            tools = await client.list_tools(request.server)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return McpInspectResponse(
            server=request.server,
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
    async def call_mcp_tool(request: McpToolCallRequest) -> McpToolCallResponse:
        client = McpSdkClient()
        try:
            result = await client.call_tool(request.server, request.tool_name, request.arguments)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return McpToolCallResponse(name=result.name, content=result.content, is_error=result.is_error)

    return app


def register_builtin_tools(registry: FrameworkRegistry, settings: AppSettings) -> None:
    register_workspace_tools(registry, settings)
    registry.register_local_tool(
        "get_current_time",
        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": "Returns the current UTC timestamp.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        handler=lambda _args, _ctx: datetime.now(UTC).isoformat(),
    )
    registry.register_local_tool(
        "ask_user",
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "Pause the current agent run and ask the user one or more structured questions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "questions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "header": {"type": "string"},
                                    "question": {"type": "string"},
                                    "message": {"type": "string"},
                                    "multiSelect": {"type": "boolean"},
                                    "allowFreeformInput": {"type": "boolean"},
                                    "maxSelections": {"type": "integer", "minimum": 1},
                                    "options": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {"type": "string"},
                                                "description": {"type": "string"},
                                                "recommended": {"type": "boolean"},
                                            },
                                            "required": ["label"],
                                        },
                                    },
                                },
                                "required": ["header", "question"],
                            },
                        },
                    },
                    "required": ["questions"],
                },
            },
        },
        handler=_ask_user_handler,
    )


def to_agent_summary(agent: AgentSpec) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        reasoning_prompt=agent.reasoning_prompt,
        reasoning_level=agent.reasoning_level,
        skills=agent.skills,
        local_tools=agent.local_tools,
        delegate_agents=agent.delegate_agents,
        capabilities=agent.capabilities,
        max_iterations=agent.max_iterations,
        provider=ProviderSummaryResponse(
            model=agent.provider.model,
            timeout_seconds=agent.provider.timeout_seconds,
        ),
    )


def _available_local_tool_summaries(
    registry: FrameworkRegistry,
    settings: AppSettings,
) -> list[LocalToolSummaryResponse]:
    default_tools = set(_default_agent_local_tools(settings))
    summaries: list[LocalToolSummaryResponse] = []
    for name in BUILTIN_AGENT_TOOLS:
        tool = registry.local_tools.get(name)
        if tool is None:
            continue
        function_payload = tool.schema.get("function", {}) if isinstance(tool.schema, dict) else {}
        description = function_payload.get("description") if isinstance(function_payload, dict) else None
        summaries.append(
            LocalToolSummaryResponse(
                name=name,
                description=description.strip() if isinstance(description, str) and description.strip() else None,
                enabled_by_default=name in default_tools,
            )
        )
    return summaries


def to_chat_session_summary_response(record: ChatSessionSummary) -> ChatSessionSummaryResponse:
    return ChatSessionSummaryResponse(**record.model_dump())


def to_chat_session_response(record: ChatSessionRecord) -> ChatSessionResponse:
    return ChatSessionResponse(
        **to_chat_session_summary_response(record).model_dump(),
        messages=[ChatSessionMessageResponse(**message.model_dump()) for message in record.messages],
        activity=[ChatSessionActivityResponse(**item.model_dump()) for item in record.activity],
    )


def _normalize_token_scopes(scopes: list[str]) -> list[str]:
    normalized = _dedupe_strings([scope.strip() for scope in scopes if isinstance(scope, str)])
    return normalized or ["agent:invoke"]


def _normalize_token_policy(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(policy or {})
    if "allowed_agents" in normalized and isinstance(normalized["allowed_agents"], list):
        normalized["allowed_agents"] = _dedupe_strings([str(value) for value in normalized["allowed_agents"]])
    if "allowed_memory_modes" in normalized and isinstance(normalized["allowed_memory_modes"], list):
        allowed_modes = [str(value) for value in normalized["allowed_memory_modes"] if str(value) in {"none", "session"}]
        normalized["allowed_memory_modes"] = _dedupe_strings(allowed_modes)
    if str(normalized.get("max_trace_level") or "") not in {"none", "steps", "debug"}:
        normalized.pop("max_trace_level", None)
    return normalized


async def _ensure_token_owner_workspace(
    session: AsyncSession,
    request: ApiTokenCreateRequest,
) -> tuple[UserRow, WorkspaceRow]:
    user_id = (request.user_id or "").strip()
    user: UserRow | None = await session.get(UserRow, user_id) if user_id else None
    if user is None:
        user = await session.scalar(select(UserRow).where(UserRow.email == request.user_email.strip()))
    if user is None:
        user = UserRow(
            id=user_id or _new_chat_item_id("user"),
            email=request.user_email.strip() or "admin@local",
            display_name=request.user_display_name.strip() or "Local Admin",
            role="admin",
            status="active",
        )
        session.add(user)
    else:
        if request.user_display_name.strip() and not user.display_name.strip():
            user.display_name = request.user_display_name.strip()
        if user.status != "active":
            user.status = "active"

    workspace_id = (request.workspace_id or "").strip()
    workspace: WorkspaceRow | None = await session.get(WorkspaceRow, workspace_id) if workspace_id else None
    workspace_slug = _safe_storage_component(request.workspace_slug, "default")
    if workspace is None:
        workspace = await session.scalar(select(WorkspaceRow).where(WorkspaceRow.slug == workspace_slug))
    if workspace is None:
        workspace = WorkspaceRow(
            id=workspace_id or _new_chat_item_id("workspace"),
            name=request.workspace_name.strip() or "Default workspace",
            slug=workspace_slug,
        )
        session.add(workspace)

    member = await session.get(WorkspaceMemberRow, (workspace.id, user.id))
    if member is None:
        session.add(WorkspaceMemberRow(workspace_id=workspace.id, user_id=user.id, role="admin"))

    return user, workspace


def _api_token_summary_response(
    token: ApiTokenRow,
    user: UserRow,
    workspace: WorkspaceRow,
) -> ApiTokenSummaryResponse:
    return ApiTokenSummaryResponse(
        id=token.id,
        name=token.name,
        user_id=token.user_id,
        user_email=user.email,
        workspace_id=token.workspace_id,
        workspace_name=workspace.name,
        token_prefix=token.token_prefix,
        scopes=list(token.scopes or []),
        policy=dict(token.policy_json or {}),
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
        created_at=token.created_at,
        updated_at=token.updated_at,
    )


async def _list_api_token_summaries(db_manager: DatabaseManager) -> list[ApiTokenSummaryResponse]:
    async with db_manager.session_factory() as session:
        rows = (
            await session.execute(
                select(ApiTokenRow, UserRow, WorkspaceRow)
                .join(UserRow, ApiTokenRow.user_id == UserRow.id)
                .join(WorkspaceRow, ApiTokenRow.workspace_id == WorkspaceRow.id)
                .order_by(ApiTokenRow.created_at.desc())
            )
        ).all()
        return [_api_token_summary_response(token, user, workspace) for token, user, workspace in rows]


async def _create_api_token(
    db_manager: DatabaseManager,
    settings: AppSettings,
    request: ApiTokenCreateRequest,
) -> ApiTokenCreateResponse:
    token, token_prefix = generate_api_token()
    token_hash = hash_api_token(token, settings.api_token_hash_pepper)
    async with db_manager.session_factory() as session:
        async with session.begin():
            user, workspace = await _ensure_token_owner_workspace(session, request)
            row = ApiTokenRow(
                id=_new_chat_item_id("token"),
                user_id=user.id,
                workspace_id=workspace.id,
                name=request.name.strip(),
                token_prefix=token_prefix,
                token_hash=token_hash,
                scopes=_normalize_token_scopes(request.scopes),
                policy_json=_normalize_token_policy(request.policy),
                expires_at=request.expires_at,
            )
            session.add(row)
        async with session.begin():
            saved = await session.get(ApiTokenRow, row.id)
            if saved is None:
                raise HTTPException(status_code=500, detail="API token was not saved")
            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner was not saved")
            summary = _api_token_summary_response(saved, user, workspace)
    return ApiTokenCreateResponse(**summary.model_dump(), token=token)


async def _revoke_api_token(db_manager: DatabaseManager, token_id: str) -> ApiTokenSummaryResponse:
    async with db_manager.session_factory() as session:
        async with session.begin():
            row = await session.get(ApiTokenRow, token_id)
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner is missing")
            return _api_token_summary_response(saved, user, workspace)


async def _resolve_public_invoke_session_id(
    db_manager: DatabaseManager,
    principal: ApiPrincipal,
    *,
    memory_mode: Literal["none", "session"],
    requested_session_id: str | None,
    run_id: str,
) -> str | None:
    if memory_mode == "none":
        if requested_session_id:
            raise HTTPException(status_code=400, detail="session_id is only allowed when memory.mode is 'session'")
        return None

    session_id = (requested_session_id or "").strip() or _new_chat_item_id("session")
    async with db_manager.session_factory() as session:
        async with session.begin():
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                session.add(
                    ChatSessionRow(
                        id=session_id,
                        owner_user_id=principal.user_id,
                        workspace_id=principal.workspace_id,
                        created_by_token_id=principal.token_id,
                    )
                )
                return session_id

            if row.owner_user_id != principal.user_id or row.workspace_id != principal.workspace_id:
                raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
            if row.created_by_token_id is None:
                row.created_by_token_id = principal.token_id
            return session_id


def _encode_public_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage_payload(final_payload: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(final_payload, dict):
        return {}
    usage = final_payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        key: int(value)
        for key, value in usage.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(value, int | float)
    }


def _public_run_completed_payload(
    *,
    run_id: str,
    agent_name: str,
    memory_mode: Literal["none", "session"],
    session_id: str | None,
    final_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "agent": agent_name,
        "memory_mode": memory_mode,
        "session_id": session_id,
        "output_text": str(final_payload.get("output_text") or ""),
        "usage": _usage_payload(final_payload),
    }


def _public_stream_events(
    event_name: str,
    payload: Any,
    *,
    trace_level: Literal["none", "steps", "debug"],
) -> list[str]:
    if event_name == "assistant":
        text = _payload_text(payload)
        return [_encode_public_sse("message.delta", {"text": text})] if text else []

    if trace_level == "none":
        return []

    if event_name in {"thought", "iteration", "context_window", "model_call"}:
        return [_encode_public_sse("trace.step", _public_trace_step_payload(event_name, payload))]

    if event_name.endswith("tool_calls"):
        return [
            _encode_public_sse("tool.call.started", item)
            for item in _public_tool_call_payloads(payload, redact_arguments=trace_level != "debug")
        ]

    if event_name.endswith("tool_results"):
        return [
            _encode_public_sse("tool.call.completed", item)
            for item in _public_tool_result_payloads(payload, redact_results=trace_level != "debug")
        ]

    return []


def _public_trace_step_payload(event_name: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"kind": event_name, "summary": str(payload)}
    summary = payload.get("summary") or payload.get("phase") or payload.get("status") or event_name
    result: dict[str, Any] = {
        "kind": str(payload.get("kind") or event_name),
        "summary": str(summary),
    }
    for key in ("iteration", "stage", "phase", "status", "elapsed_ms"):
        if key in payload:
            result[key] = payload[key]
    return result


def _public_tool_call_payloads(payload: Any, *, redact_arguments: bool) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    results: list[dict[str, Any]] = []
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        result = {
            "id": item.get("id"),
            "name": item.get("name"),
        }
        result["arguments"] = "[redacted]" if redact_arguments else item.get("arguments", {})
        results.append(result)
    return results


def _public_tool_result_payloads(payload: Any, *, redact_results: bool) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    tool_results = payload.get("results")
    if not isinstance(tool_results, list):
        return []
    results: list[dict[str, Any]] = []
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        result = {
            "id": item.get("tool_call_id"),
            "name": item.get("name"),
            "status": "error" if item.get("is_error") else "ok",
        }
        result["summary"] = "[redacted]" if redact_results else _summarize_public_tool_result(item.get("content"))
        results.append(result)
    return results


def _summarize_public_tool_result(content: Any, *, max_chars: int = 600) -> str:
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


async def _record_public_agent_run(
    db_manager: DatabaseManager,
    *,
    principal: ApiPrincipal,
    run_id: str,
    agent_name: str,
    memory_mode: Literal["none", "session"],
    session_id: str | None,
    status: str,
    latency_ms: int,
    provider: str,
    model: str,
    usage: dict[str, Any],
    error: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    async with db_manager.session_factory() as session:
        async with session.begin():
            session.add(
                AgentRunLogRow(
                    id=run_id,
                    user_id=principal.user_id,
                    token_id=principal.token_id,
                    workspace_id=principal.workspace_id,
                    agent_name=agent_name,
                    memory_mode=memory_mode,
                    session_id=session_id,
                    status=status,
                    latency_ms=latency_ms,
                    provider=provider,
                    model=model,
                    usage_json=dict(usage or {}),
                    error_json=dict(error or {}),
                    metadata_json=dict(metadata or {}),
                )
            )


def _new_chat_item_id(prefix: str) -> str:
    return f"{prefix}-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid4().hex[:8]}"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _safe_storage_component(raw: str, default: str) -> str:
    normalized = _SAFE_STORAGE_COMPONENT_RE.sub("-", raw.strip()).strip("._-")
    return normalized or default


def _attachment_session_dir(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / ".agent_framework" / "attachments" / _safe_storage_component(session_id, "session")


def _chat_upload_visible_root(settings: AppSettings, session_id: str) -> Path:
    if settings.session_workspace_enabled:
        return settings.session_workspace_dir(session_id)
    return settings.workspace_root()


def _chat_upload_session_dir(settings: AppSettings, session_id: str) -> Path:
    visible_root = _chat_upload_visible_root(settings, session_id)
    if settings.session_workspace_enabled:
        return visible_root / "uploads"
    return visible_root / ".agent_framework" / "uploads" / _safe_storage_component(session_id, "session")


def _download_session_dir(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / ".agent_framework" / "downloads" / _safe_storage_component(session_id, "session")


def _safe_uploaded_filename(raw_name: str, default_stem: str) -> str:
    candidate = Path(raw_name).name.strip()
    if not candidate:
        candidate = default_stem
    parsed = Path(candidate)
    safe_stem = _safe_storage_component(parsed.stem, default_stem)
    safe_suffix = _SAFE_STORAGE_COMPONENT_RE.sub("", parsed.suffix)
    if safe_suffix and not safe_suffix.startswith("."):
        safe_suffix = f".{safe_suffix}"
    return f"{safe_stem}{safe_suffix}"


def _next_available_upload_path(directory: Path, file_name: str) -> Path:
    candidate = directory / file_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        next_candidate = directory / f"{stem}-{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def _build_user_transcript_message(request: AgentRunRequest) -> ChatTranscriptMessage:
    metadata = request.metadata or {}
    content = _request_display_input(request).strip() or "Message sent."
    attachments = metadata.get("attachments") if isinstance(metadata.get("attachments"), list) else []
    normalized_attachments = [item for item in attachments if isinstance(item, dict)]
    message_id = str(metadata.get("user_message_id") or _new_chat_item_id("user"))
    return ChatTranscriptMessage(id=message_id, role="user", content=content, attachments=normalized_attachments)


def _payload_text(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("text")
        return "" if value is None else str(value)
    return ""


def _payload_output_text(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("output_text")
        return "" if value is None else str(value)
    return ""


def _upsert_assistant_transcript(messages: list[ChatTranscriptMessage], message_id: str, text: str) -> None:
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].content += text
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content=text))


def _replace_assistant_transcript(messages: list[ChatTranscriptMessage], message_id: str, text: str) -> None:
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].content = text
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content=text))


def _append_assistant_attachments(
    messages: list[ChatTranscriptMessage],
    message_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    if not attachments:
        return
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].attachments = _merge_attachment_metadata(messages[-1].attachments, attachments)
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content="", attachments=attachments))


def _merge_attachment_metadata(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        if not isinstance(item, dict):
            continue
        key = _attachment_metadata_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _attachment_metadata_key(item: dict[str, Any]) -> str:
    for field in ("id", "download_url", "workspace_path", "name"):
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _published_download_attachments_from_tool_results(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    attachments: list[dict[str, Any]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        if raw_result.get("name") != "publish_downloadable_file" or bool(raw_result.get("is_error")):
            continue
        content = _tool_content_json_object(raw_result.get("content"))
        if content is None:
            continue
        download_url = content.get("download_url")
        name = content.get("name")
        if not isinstance(download_url, str) or not download_url.strip() or not isinstance(name, str) or not name.strip():
            continue
        content_type = str(content.get("content_type") or "application/octet-stream")
        attachments.append(
            {
                "id": content.get("id") or f"download-{name}",
                "name": name,
                "size": _coerce_int(content.get("size")),
                "type": content_type,
                "content_type": content_type,
                "last_modified": 0,
                "workspace_path": content.get("workspace_path"),
                "download_url": download_url,
                "uploaded_at": content.get("published_at"),
                "summary": content.get("summary") or "Generated by the agent and ready to download.",
                "kind": _attachment_kind_for_content_type(content_type),
            }
        )
    return attachments


def _tool_content_json_object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _attachment_kind_for_content_type(content_type: str) -> str:
    normalized = content_type.strip().lower()
    if normalized == "application/pdf":
        return "pdf"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("text/"):
        return "text"
    return "binary"


def _build_session_preview(messages: list[ChatTranscriptMessage]) -> str:
    for message in reversed(messages):
        if message.content.strip():
            preview = " ".join(message.content.strip().split())
            return preview[:200]
    return ""


def _fallback_session_title(messages: list[ChatTranscriptMessage]) -> str:
    seed = next((message.content for message in messages if message.role == "user" and message.content.strip()), "")
    normalized = " ".join(seed.replace("\n", " ").split()).strip(" -:,.\t")
    if not normalized:
        return "New conversation"
    if len(normalized) <= 48:
        return normalized
    clipped = normalized[:48].rstrip(" ,.:;-")
    return f"{clipped}..."


def _normalize_generated_title(value: str) -> str:
    cleaned = value.strip().strip('"').strip("'")
    cleaned = cleaned.replace("\n", " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rstrip(" ,.:;-")
    return cleaned


async def _generate_session_title(
    registry: FrameworkRegistry,
    agent: AgentSpec,
    messages: list[ChatTranscriptMessage],
) -> str:
    fallback = _fallback_session_title(messages)
    first_user_message = next((message.content for message in messages if message.role == "user" and message.content.strip()), "")
    if not first_user_message:
        return fallback
    try:
        adapter = registry.get_model_provider(agent.provider)
        response = await adapter.generate(
            GenerationRequest(
                model=agent.provider.model,
                system_prompt=(
                    "Generate a concise conversation title. "
                    "Return plain text only, no quotes, no punctuation wrapper, 3 to 8 words."
                ),
                messages=[Message(role="user", content=first_user_message)],
                temperature=0.0,
                max_tokens=24,
            )
        )
    except Exception:
        return fallback
    return _normalize_generated_title(response.output_text) or fallback


def _ask_user_handler(args: dict[str, Any], _ctx: RunContext | None) -> UserInputRequest:
    raw_questions = args.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("ask_user requires a non-empty 'questions' array")

    questions: list[UserQuestion] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            raise ValueError("ask_user questions must be objects")
        normalized_item = dict(item)
        if "multiSelect" in normalized_item:
            normalized_item["multi_select"] = normalized_item.pop("multiSelect")
        if "allowFreeformInput" in normalized_item:
            normalized_item["allow_freeform_input"] = normalized_item.pop("allowFreeformInput")
        if "maxSelections" in normalized_item:
            normalized_item["max_selections"] = normalized_item.pop("maxSelections")
        if isinstance(normalized_item.get("options"), list):
            normalized_item["options"] = [
                UserQuestionOption.model_validate(option).model_dump(mode="python")
                for option in normalized_item["options"]
                if isinstance(option, dict)
            ]
        questions.append(UserQuestion.model_validate(normalized_item))

    title = str(args.get("title") or "Additional input required").strip() or "Additional input required"
    return UserInputRequest(
        id=_new_chat_item_id("question"),
        tool_name="ask_user",
        title=title,
        questions=questions,
    )


def _extract_pending_user_input(activity: list[ChatActivityItem]) -> UserInputRequest | None:
    resolved_ids: set[str] = set()
    for item in reversed(activity):
        if item.title == SSE_EVENT_INPUT_RESOLVED and isinstance(item.payload, dict):
            resolved_id = str(item.payload.get("id", "")).strip()
            if resolved_id:
                resolved_ids.add(resolved_id)
            continue
        if item.title != SSE_EVENT_INPUT_REQUIRED:
            continue
        try:
            request = UserInputRequest.model_validate(item.payload)
        except Exception:
            continue
        if request.id not in resolved_ids:
            return request
    return None


def _build_resume_tool_result(
    request: AgentRunRequest,
    pending_input: UserInputRequest | None,
) -> ResumedToolResult | None:
    metadata = request.metadata or {}
    resume_question_id = str(metadata.get("resume_question_id") or "").strip()
    if not resume_question_id:
        return None
    if pending_input is None:
        raise HTTPException(status_code=409, detail="This session does not have a pending question to answer")
    if pending_input.id != resume_question_id:
        raise HTTPException(status_code=409, detail="The pending question no longer matches the submitted answer")

    raw_answers = metadata.get("question_response")
    answers = raw_answers if isinstance(raw_answers, dict) else {"response": request.input}
    normalized_answers = {str(key): value for key, value in answers.items()}
    summary = _request_display_input(request).strip() or "Message sent."
    return ResumedToolResult(
        tool_call_id=pending_input.tool_call_id,
        tool_name=pending_input.tool_name,
        request_id=pending_input.id,
        answers=normalized_answers,
        summary=summary,
    )


def _request_display_input(request: AgentRunRequest) -> str:
    metadata = request.metadata or {}
    raw_display = metadata.get("display_input")
    if isinstance(raw_display, str):
        normalized = raw_display.strip()
        if normalized:
            return normalized
    if isinstance(request.input, str):
        return request.input
    text_parts: list[str] = []
    image_count = 0
    for item in request.input:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                text_parts.append(text)
        elif item.get("type") == "image_url":
            image_count += 1
    if text_parts:
        return "\n\n".join(text_parts)
    if image_count:
        suffix = "s" if image_count != 1 else ""
        return f"Shared {image_count} image attachment{suffix}."
    return "Message sent."


async def _apply_runtime_config(app: FastAPI, kind: ConfigKind, payload: list[dict[str, object]]) -> None:
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store
    settings: AppSettings = app.state.settings
    provider_config = await _resolve_default_provider(settings, config_store)

    if kind == "providers":
        mcp_payload = await config_store.get_document("mcp")
        agent_payload = await config_store.get_document("agents")
        provider_config = await _resolve_default_provider(settings, config_store, payload)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(agent_payload, provider_config, _parse_mcp_servers(mcp_payload), settings)
        }
        return

    if kind == "mcp":
        registry.mcp_servers.clear()
        if settings.mcp_enabled and registry.mcp_client is None:
            registry.set_mcp_client(McpSdkClient())
        for server in _parse_mcp_servers(payload):
            registry.register_mcp_server(server)
        agent_payload = await config_store.get_document("agents")
        provider_config = await _resolve_default_provider(settings, config_store)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(agent_payload, provider_config, _parse_mcp_servers(payload), settings)
        }
        return

    if kind == "skill_sources":
        await _reload_git_skills(app, payload)
        return

    if kind == "agents":
        mcp_payload = await config_store.get_document("mcp")
        provider_config = await _resolve_default_provider(settings, config_store)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(payload, provider_config, _parse_mcp_servers(mcp_payload), settings)
        }
        return


def _normalize_management_kind(kind: str) -> ManagementKind:
    if kind not in {"agents", "mcp", "skills"}:
        raise HTTPException(status_code=404, detail=f"Unknown management kind: {kind}")
    return kind


def _normalize_management_export_format(value: str) -> ManagementExportFormat:
    normalized = value.strip().lower()
    if normalized not in {"yaml", "json"}:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {value}")
    return normalized  # type: ignore[return-value]


async def _build_management_export_payload(
    app: FastAPI,
    kind: ManagementKind,
) -> tuple[dict[str, Any], int]:
    settings: AppSettings = app.state.settings
    config_store: ConfigStore = app.state.config_store
    exported_at = datetime.now(UTC).isoformat()

    if kind in {"agents", "mcp"}:
        raw_items = await config_store.get_document(kind)
        items = [
            _normalize_agent_payload_item(item, settings) if kind == "agents" else item
            for item in raw_items
        ]
        return {
            "version": 1,
            "kind": kind,
            "exported_at": exported_at,
            "items": items,
        }, len(items)

    payload = await _build_skill_management_export_payload(app)
    return payload, len(payload.get("items", []))


async def _build_skill_management_export_payload(app: FastAPI) -> dict[str, Any]:
    registry: FrameworkRegistry = app.state.registry
    settings: AppSettings = app.state.settings
    config_store: ConfigStore = app.state.config_store
    skill_sources = await config_store.get_document("skill_sources")

    items: list[dict[str, Any]] = []
    for skill_name in sorted(registry.skills):
        manifest = registry.manifest_skills.get(skill_name)
        if manifest is None:
            inline_skill = registry.skills[skill_name]
            items.append(
                SkillManagementItemResponse(
                    name=inline_skill.name,
                    enabled=registry.is_skill_enabled(skill_name),
                    category="unknown",
                    source_type="local",
                    version=str(inline_skill.metadata.get("version", "")),
                    description=inline_skill.description,
                    source=SkillManagementSourceResponse(type="inline", category="unknown"),
                ).model_dump(mode="json")
            )
            continue

        category = _skill_category(manifest, settings)
        items.append(
            SkillManagementItemResponse(
                name=manifest.name,
                enabled=registry.is_skill_enabled(skill_name),
                category=category,
                source_type=manifest.source_type,
                version=manifest.version,
                description=manifest.description,
                source=_build_skill_management_source(manifest, category, skill_sources),
            ).model_dump(mode="json")
        )

    return {
        "version": 1,
        "kind": "skills",
        "exported_at": datetime.now(UTC).isoformat(),
        "notes": [
            "This export captures skill enablement plus git skill source configuration.",
            "Uploaded, authored, and other local skills are referenced by name only; their bundle files are not embedded.",
        ],
        "skill_sources": skill_sources,
        "items": items,
    }


def _build_skill_management_source(
    spec: ManifestSkillSpec,
    category: str,
    skill_sources: list[dict[str, object]],
) -> SkillManagementSourceResponse:
    if spec.source_type == "git":
        matched_source = next((item for item in skill_sources if _matches_skill_source(spec, item)), None)
        return SkillManagementSourceResponse(
            type="git",
            category="github_synced",
            url=spec.git_url,
            ref=spec.git_ref,
            subdir=str(matched_source.get("subdir")) if matched_source and matched_source.get("subdir") else None,
            name=str(matched_source.get("name")) if matched_source and matched_source.get("name") else None,
        )

    if category == "built_in":
        return SkillManagementSourceResponse(type="built_in", category="built_in")
    if category in {"uploaded", "authored"}:
        return SkillManagementSourceResponse(type="managed", category=category)
    return SkillManagementSourceResponse(type="unknown", category="unknown")


def _serialize_management_export_payload(
    payload: dict[str, Any],
    export_format: ManagementExportFormat,
) -> str:
    if export_format == "yaml":
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"


async def _import_management_payload(
    app: FastAPI,
    kind: ManagementKind,
    raw_text: str,
    file_name: str | None,
) -> ManagementImportResponse:
    settings: AppSettings = app.state.settings
    config_store: ConfigStore = app.state.config_store
    parsed = _parse_management_upload(raw_text, file_name)

    if kind in {"agents", "mcp"}:
        raw_items = _extract_management_items(kind, parsed)
        validated = _validate_config_payload(kind, raw_items, settings)
        saved = await config_store.save_document(kind, validated)
        await _apply_runtime_config(app, kind, saved)
        label = "agents" if kind == "agents" else "MCP services"
        return ManagementImportResponse(
            kind=kind,
            imported_items=len(validated),
            applied_items=len(saved),
            summary=f"Imported {len(saved)} {label}.",
        )

    return await _import_skill_management_payload(app, parsed)


def _parse_management_upload(raw_text: str, file_name: str | None) -> Any:
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="Imported file is empty")
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        target_name = file_name or "uploaded file"
        raise HTTPException(status_code=400, detail=f"Could not parse {target_name}: {exc}") from exc
    if parsed is None:
        raise HTTPException(status_code=400, detail="Imported file did not contain any configuration data")
    return parsed


def _extract_management_items(kind: ConfigKind, payload: Any) -> list[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Imported configuration must be a YAML/JSON object or array")

    payload_kind = payload.get("kind")
    if isinstance(payload_kind, str) and payload_kind and payload_kind != kind:
        raise HTTPException(status_code=400, detail=f"Imported file is for '{payload_kind}', not '{kind}'")

    items = payload.get("items")
    if items is None and isinstance(payload.get("data"), list):
        items = payload.get("data")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Imported configuration must include an 'items' array")
    return items


async def _import_skill_management_payload(
    app: FastAPI,
    payload: Any,
) -> ManagementImportResponse:
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store

    imported_items, imported_sources = _extract_skill_management_payload(payload)
    saved_sources = await config_store.save_document("skill_sources", imported_sources)
    await _apply_runtime_config(app, "skill_sources", saved_sources)

    warnings: list[str] = []
    applied_items = 0
    seen_names: set[str] = set()

    for item in imported_items:
        if item.name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate skill entry: {item.name}")
        seen_names.add(item.name)

        if item.name not in registry.skills:
            warnings.append(
                (
                    f"Skill '{item.name}' is not installed in this workspace. "
                    "Its enabled state was not applied."
                )
            )
            continue

        registry.set_skill_enabled(item.name, item.enabled)
        await config_store.set_skill_enabled(item.name, item.enabled)
        if not item.enabled and registry.skill_process_manager is not None:
            await registry.skill_process_manager.stop_skill(item.name)
        applied_items += 1

    await _reconcile_skill_process_manager(registry)

    summary = (
        f"Imported {len(imported_items)} skill entries and synced {len(saved_sources)} git skill sources. "
        f"Applied state to {applied_items} installed skills."
    )
    return ManagementImportResponse(
        kind="skills",
        imported_items=len(imported_items),
        applied_items=applied_items,
        summary=summary,
        warnings=warnings,
    )


def _extract_skill_management_payload(
    payload: Any,
) -> tuple[list[SkillManagementItemResponse], list[dict[str, object]]]:
    if isinstance(payload, list):
        if all(_looks_like_skill_source_entry(item) for item in payload):
            return [], _validate_config_payload("skill_sources", payload)
        imported_items = _validate_skill_management_items(payload)
        return imported_items, _derive_skill_sources_from_skill_items(imported_items)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Imported skill configuration must be a YAML/JSON object or array")

    payload_kind = payload.get("kind")
    if isinstance(payload_kind, str) and payload_kind not in {"skills", "skill_sources"}:
        raise HTTPException(status_code=400, detail=f"Imported file is for '{payload_kind}', not 'skills'")

    if payload_kind == "skill_sources":
        raw_sources = payload.get("items")
        if raw_sources is None and isinstance(payload.get("data"), list):
            raw_sources = payload.get("data")
        if raw_sources is None:
            raw_sources = payload.get("skill_sources")
        if not isinstance(raw_sources, list):
            raise HTTPException(status_code=400, detail="Imported skill source payload must include an array of sources")
        return [], _validate_config_payload("skill_sources", raw_sources)

    items_raw = payload.get("items")
    if items_raw is None and isinstance(payload.get("data"), list):
        items_raw = payload.get("data")
    if items_raw is None:
        items_raw = []
    if not isinstance(items_raw, list):
        raise HTTPException(status_code=400, detail="Imported skill configuration 'items' field must be an array")

    imported_items = _validate_skill_management_items(items_raw)
    raw_sources = payload.get("skill_sources")
    if raw_sources is None:
        return imported_items, _derive_skill_sources_from_skill_items(imported_items)
    if not isinstance(raw_sources, list):
        raise HTTPException(status_code=400, detail="Imported skill configuration 'skill_sources' field must be an array")
    return imported_items, _validate_config_payload("skill_sources", raw_sources)


def _validate_skill_management_items(items: list[object]) -> list[SkillManagementItemResponse]:
    validated: list[SkillManagementItemResponse] = []
    seen_names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Skill entries must be objects")
        try:
            parsed = SkillManagementItemResponse.model_validate(item)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid skill entry: {exc}") from exc
        if parsed.name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate skill entry: {parsed.name}")
        seen_names.add(parsed.name)
        validated.append(parsed)
    return validated


def _looks_like_skill_source_entry(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("url"), str) and (
        item.get("source_type") == "git" or item.get("category") == "github_synced"
    )


def _derive_skill_sources_from_skill_items(items: list[SkillManagementItemResponse]) -> list[dict[str, object]]:
    derived_sources: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str | None, str | None]] = set()
    for item in items:
        if item.source.type != "git" or not item.source.url:
            continue
        source = PersistedSkillSourceConfig(
            source_type="git",
            category="github_synced",
            name=item.source.name,
            url=item.source.url,
            ref=item.source.ref,
            subdir=item.source.subdir,
        ).model_dump(mode="json")
        dedupe_key = (str(source["url"]), source.get("ref"), source.get("subdir"))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        derived_sources.append(source)
    return derived_sources


def _normalize_config_kind(kind: str) -> ConfigKind:
    if kind not in {"agents", "mcp", "skill_sources", "providers"}:
        raise HTTPException(status_code=404, detail=f"Unknown config kind: {kind}")
    return kind


def _config_document_response(kind: ConfigKind, payload: list[dict[str, object]], settings: AppSettings) -> ConfigDocumentResponse:
    label_map = {"agents": "Agents", "mcp": "MCP Servers", "skill_sources": "Skill Sources", "providers": "Providers"}
    normalized_payload = [
        _normalize_agent_payload_item(item, settings) if kind == "agents" else item
        for item in payload
    ]
    if kind == "providers":
        normalized_payload = [_mask_provider_api_key(item) for item in normalized_payload]
    return ConfigDocumentResponse(
        kind=kind,
        label=label_map[kind],
        filePath=f"postgres://config/{kind}",
        raw=f"{json.dumps(normalized_payload, ensure_ascii=False, indent=2)}\n",
        exampleRaw=_example_config_raw(kind, settings),
        data=normalized_payload,
    )


def _example_config_raw(kind: ConfigKind, settings: AppSettings) -> str:
    if kind == "providers":
        example = {
            "name": "my-provider",
            "provider_type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-...",
            "default_model": "gpt-4.1",
            "position": 0,
        }
        return f"{json.dumps([example], ensure_ascii=False, indent=2)}\n"
    raw_map = {
        "agents": settings.agents_json,
        "mcp": settings.mcp_servers_json,
        "skill_sources": settings.skill_sources_json,
    }
    raw = raw_map[kind]
    payload = json.loads(raw) if raw else []
    if kind == "agents":
        payload = _validate_config_payload("agents", payload or [], settings)
    return f"{json.dumps(payload or [], ensure_ascii=False, indent=2)}\n"


def _seed_mcp_payload(settings: AppSettings) -> list[dict[str, object]]:
    if not settings.mcp_servers_json:
        return []
    return json.loads(settings.mcp_servers_json)


def _seed_skill_source_payload(settings: AppSettings) -> list[dict[str, object]]:
    if not settings.skill_sources_json:
        return []
    raw = json.loads(settings.skill_sources_json)
    return _validate_config_payload("skill_sources", raw)


def _seed_agent_payload(
    settings: AppSettings,
    provider_config: ProviderConfig,
    mcp_servers: list[McpServerConfig],
) -> list[dict[str, object]]:
    raw = json.loads(settings.agents_json) if settings.agents_json else None
    if raw is not None:
        return _validate_config_payload("agents", raw, settings)

    default_item = PersistedAgentConfig(
        name="default",
        description=settings.agent_description,
        system_prompt=settings.agent_system_prompt,
        reasoning_prompt=settings.reasoning_skill_instructions,
        provider=provider_config,
        skills=[],
        local_tools=_default_agent_local_tools(settings),
        mcp_servers=[server.name for server in mcp_servers],
        capabilities={Capability.CHAT, Capability.REACT, Capability.TOOL_CALLING, Capability.STREAMING},
        max_iterations=settings.default_max_iterations,
    )
    return [default_item.model_dump(mode="json")]


def _validate_config_payload(kind: ConfigKind, payload: list[object], settings: AppSettings | None = None) -> list[dict[str, object]]:
    if kind == "mcp":
        return [McpServerConfig.model_validate(item).model_dump(mode="json") for item in payload]

    if kind == "providers":
        from agent_framework.infra.config_store import PersistedProviderConfig
        normalized_providers = [PersistedProviderConfig.model_validate(item).model_dump(mode="json") for item in payload]
        default_model_names = [
            str(item.get("name") or "")
            for item in normalized_providers
            if str(item.get("default_model") or "").strip()
        ]
        if len(default_model_names) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only one provider may declare a default_model. "
                    f"Found: {', '.join(default_model_names)}"
                ),
            )

        if default_model_names:
            default_name = default_model_names[0]
            return [
                {
                    **item,
                    "default_model": str(item.get("default_model") or "").strip(),
                    "is_default": str(item.get("name") or "") == default_name,
                }
                for item in normalized_providers
            ]

        legacy_default_names = [
            str(item.get("name") or "")
            for item in normalized_providers
            if bool(item.get("is_default"))
        ]
        if len(legacy_default_names) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only one provider may be marked as default. "
                    f"Found: {', '.join(legacy_default_names)}"
                ),
            )

        return [
            {
                **item,
                "default_model": str(item.get("default_model") or "").strip(),
            }
            for item in normalized_providers
        ]

    if kind == "skill_sources":
        normalized_sources: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="Skill source entries must be JSON objects")
            normalized = normalize_git_source_payload(item)
            if normalized is None:
                raise HTTPException(status_code=400, detail="Only git skill sources are currently supported")
            normalized_sources.append(PersistedSkillSourceConfig.model_validate(normalized).model_dump(mode="json"))
        return normalized_sources

    seen_names: set[str] = set()
    normalized: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Agent config entries must be JSON objects")
        normalized_item = dict(item)
        mcp_refs = normalized_item.get("mcp_servers", [])
        if isinstance(mcp_refs, list):
            normalized_item["mcp_servers"] = [
                ref if isinstance(ref, str) else str(ref.get("name"))
                for ref in mcp_refs
                if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("name"))
            ]
        tool_refs = normalized_item.get("mcp_tools", [])
        if isinstance(tool_refs, list):
            normalized_item["mcp_tools"] = [
                McpToolReference.model_validate(tool_ref).model_dump(mode="json")
                for tool_ref in tool_refs
                if isinstance(tool_ref, dict)
            ]
        normalized_item = _normalize_agent_payload_item(normalized_item, settings)
        agent = PersistedAgentConfig.model_validate(normalized_item)
        if agent.name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate agent name: {agent.name}")
        referenced_servers = {tool.server_name for tool in agent.mcp_tools}
        missing_server_refs = sorted(referenced_servers.difference(agent.mcp_servers))
        if missing_server_refs:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Agent '{agent.name}' has MCP tool selections for unselected servers: {', '.join(missing_server_refs)}"
                ),
            )
        seen_names.add(agent.name)
        normalized.append(agent.model_dump(mode="json"))
    return normalized


def _extract_agent_renames(metadata: object) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}

    raw_renames = metadata.get("agent_renames")
    if not isinstance(raw_renames, list):
        return {}

    rename_map: dict[str, str] = {}
    for item in raw_renames:
        if not isinstance(item, dict):
            continue
        old_name = item.get("old_name")
        new_name = item.get("new_name")
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            continue
        old_normalized = old_name.strip()
        new_normalized = new_name.strip()
        if not old_normalized or not new_normalized or old_normalized == new_normalized:
            continue
        rename_map[old_normalized] = new_normalized

    return rename_map


def _parse_mcp_servers(payload: list[dict[str, object]]) -> list[McpServerConfig]:
    return [McpServerConfig.model_validate(item) for item in payload]


async def _reload_git_skills(app: FastAPI, payload: list[dict[str, object]]) -> None:
    registry: FrameworkRegistry = app.state.registry
    loader: SkillLoader = app.state.skill_loader
    config_store: ConfigStore = app.state.config_store

    existing_git_skills = [name for name, spec in registry.manifest_skills.items() if spec.source_type == "git"]
    if registry.skill_process_manager:
        for skill_name in existing_git_skills:
            await registry.skill_process_manager.stop_skill(skill_name)
    for skill_name in existing_git_skills:
        registry.unregister_skill(skill_name)

    for spec in await loader.discover_git(payload):
        registry.register_manifest_skill(spec)

    await _sync_registry_skill_states(registry, config_store)
    await _reconcile_skill_process_manager(registry)


def _infer_skill_install_source_type(settings: AppSettings, source: str) -> str:
    resolved = settings.resolve_path(source)
    if resolved is not None and resolved.is_dir():
        return "directory"
    return "git"


def _detect_uploaded_skill_directory(root: Path) -> Path | None:
    if (root / "SKILL.md").is_file() or (root / "skill.yaml").is_file():
        return root

    candidates = sorted(
        {
            path.parent
            for pattern in ("SKILL.md", "skill.yaml")
            for path in root.rglob(pattern)
            if ".git" not in path.parts and "__MACOSX" not in path.parts
        }
    )
    if len(candidates) != 1:
        return None
    return candidates[0]


def _find_matching_git_skill(
    registry: FrameworkRegistry,
    source_payload: dict[str, object],
) -> ManifestSkillSpec | None:
    for spec in registry.manifest_skills.values():
        if spec.source_type != "git":
            continue
        if spec.git_url != source_payload.get("url"):
            continue
        if spec.git_ref != source_payload.get("ref"):
            continue
        source_dir = Path(spec.source_dir or "")
        subdir = source_payload.get("subdir")
        if subdir and subdir not in source_dir.as_posix():
            continue
        return spec
    return None


def _matches_skill_source(spec: ManifestSkillSpec, payload: dict[str, object]) -> bool:
    return spec.git_url == payload.get("url") and spec.git_ref == payload.get("ref") and bool(
        not payload.get("subdir") or str(payload.get("subdir")) in (spec.source_dir or "")
    )


def _skill_category(spec: ManifestSkillSpec, settings: AppSettings) -> str:
    if spec.source_type == "git":
        return "github_synced"
    if not spec.source_dir:
        return "unknown"
    source_dir = Path(spec.source_dir).resolve()
    for category in ("built_in", "uploaded", "authored"):
        managed_dir = settings.managed_skill_directory(category).resolve()
        if source_dir == managed_dir or source_dir.is_relative_to(managed_dir):
            return category
    return "unknown"


def _build_agent_specs(
    payload: list[dict[str, object]],
    provider_config: ProviderConfig,
    mcp_servers: list[McpServerConfig],
    settings: AppSettings,
) -> list[AgentSpec]:
    mcp_by_name = {server.name: server for server in mcp_servers}
    agents: list[AgentSpec] = []
    for item in payload:
        persisted = PersistedAgentConfig.model_validate(_normalize_agent_payload_item(item, settings))
        resolved_mcp = [mcp_by_name[name] for name in persisted.mcp_servers if name in mcp_by_name]
        agents.append(
            AgentSpec(
                name=persisted.name,
                description=persisted.description,
                system_prompt=persisted.system_prompt,
                reasoning_prompt=persisted.reasoning_prompt,
                reasoning_level=persisted.reasoning_level,
                provider=_merge_provider_config(persisted.provider, provider_config),
                skills=persisted.skills,
                local_tools=[t for t in _dedupe_strings(persisted.local_tools) if t != "echo"],
                delegate_agents=persisted.delegate_agents,
                mcp_servers=resolved_mcp,
                mcp_tools=persisted.mcp_tools,
                capabilities=persisted.capabilities,
                max_iterations=persisted.max_iterations,
                metadata=dict(persisted.metadata),
            )
        )
    return agents


async def _resolve_default_provider(
    settings: AppSettings,
    config_store: ConfigStore,
    providers_payload: list[dict[str, object]] | None = None,
) -> ProviderConfig:
    if providers_payload is None:
        try:
            providers_payload = await config_store.get_document("providers")
        except Exception:
            providers_payload = []

    from agent_framework.infra.config_store import PersistedProviderConfig

    for item in providers_payload or []:
        default_model = str(item.get("default_model") or "").strip()
        if default_model and item.get("base_url"):
            cfg = PersistedProviderConfig.model_validate(item)
            return ProviderConfig(
                provider=cfg.provider_type,
                model=default_model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout_seconds=settings.request_timeout_seconds,
            )

    for item in providers_payload or []:
        if item.get("is_default") and item.get("base_url"):
            cfg = PersistedProviderConfig.model_validate(item)
            return ProviderConfig(
                provider=cfg.provider_type,
                model=settings.default_model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout_seconds=settings.request_timeout_seconds,
            )

    return default_provider_config(settings)


def _format_api_key_masked(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:5]}...{key[-3:]}"


def _mask_provider_api_key(item: dict[str, object]) -> dict[str, object]:
    masked = dict(item)
    if masked.get("api_key"):
        key = str(masked["api_key"])
        masked["has_api_key"] = bool(key)
        masked["api_key_masked"] = _format_api_key_masked(key) if key else None
        masked["api_key"] = None
    else:
        masked["has_api_key"] = False
        masked["api_key_masked"] = None
    return masked


def _merge_provider_config(
    provider: ProviderConfig,
    default_provider: ProviderConfig,
) -> ProviderConfig:
    return ProviderConfig(
        provider=provider.provider or default_provider.provider,
        model=provider.model or default_provider.model,
        api_key=provider.api_key or default_provider.api_key,
        base_url=provider.base_url or default_provider.base_url,
        timeout_seconds=provider.timeout_seconds or default_provider.timeout_seconds,
        extra={**default_provider.extra, **provider.extra},
    )


async def _sync_registry_skill_states(registry: FrameworkRegistry, config_store: ConfigStore) -> None:
    registry.sync_skill_enabled_states(await config_store.get_skill_state_map())


async def _reconcile_skill_process_manager(registry: FrameworkRegistry) -> None:
    if registry.has_executable_skills(enabled_only=True):
        if registry.skill_process_manager is None:
            registry.skill_process_manager = SkillProcessManager()
            await registry.skill_process_manager.start()
        return

    if registry.skill_process_manager is not None:
        await registry.skill_process_manager.stop()
        registry.skill_process_manager = None


async def _set_skill_enabled(app: FastAPI, skill_name: str, enabled: bool) -> None:
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store

    if skill_name not in registry.skills:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    registry.set_skill_enabled(skill_name, enabled)
    await config_store.set_skill_enabled(skill_name, enabled)

    if not enabled and registry.skill_process_manager is not None:
        await registry.skill_process_manager.stop_skill(skill_name)

    await _reconcile_skill_process_manager(registry)


def _language_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "javascript"
    if suffix == ".json":
        return "json"
    return "text"


def _resolve_skill_preview_paths(
    spec: ManifestSkillSpec,
    settings: AppSettings,
) -> tuple[Path | None, Path | None]:
    if not spec.source_dir:
        return None, None

    source_dir = Path(spec.source_dir).resolve()
    if not source_dir.is_dir():
        return None, None

    if spec.source_type == "git":
        managed_dir = settings.managed_skill_directory("github_synced").resolve()
        for candidate in [source_dir, *source_dir.parents]:
            if candidate.parent == managed_dir:
                return candidate, managed_dir

    return source_dir, source_dir


def _collect_skill_preview_files(
    source_dir: Path,
    path_base: Path | None = None,
) -> list[SkillPreviewFileResponse]:
    if not source_dir.is_dir():
        return []

    display_base = path_base or source_dir
    files: list[SkillPreviewFileResponse] = []
    for file_path in sorted(path for path in source_dir.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(source_dir)
        if any(part in _SKILL_PREVIEW_IGNORED_DIRS for part in relative_path.parts[:-1]):
            continue
        content = _read_skill_preview_text(file_path)
        if content is None:
            continue
        files.append(
            SkillPreviewFileResponse(
                path=str(file_path.relative_to(display_base)).replace("\\", "/"),
                language=_language_from_path(file_path),
                content=content,
            )
        )
    return files


def _read_skill_preview_text(file_path: Path) -> str | None:
    raw = file_path.read_bytes()
    if b"\x00" in raw:
        return None
    preview_bytes = raw[:_SKILL_PREVIEW_MAX_BYTES]
    try:
        content = preview_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(raw) > _SKILL_PREVIEW_MAX_BYTES:
        return f"{content}\n\n... truncated ...\n"
    return content
