from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from covalent.application.errors import ApplicationError
from covalent.application.services.agent_invocation import AgentInvocationService
from covalent.application.services.delegate_service import (
    DelegateService,
    register_ask_parent_tool,
    register_delegate_lifecycle_tools,
    register_local_answer_from_delegate_tool,
    run_startup_sweeps,
)
from covalent.api._auth_helpers import ConsoleAuthGuardMiddleware
from covalent.api._shared import _sandbox_reaper_loop
from covalent.application.services.invoke_service import _ApiTokenRunLimiter
from covalent.application.services.management_service import build_registry
from covalent.application.services.sandbox_binding_service import SandboxBindingService
from covalent.application.services.sandbox_profile_service import SandboxProfileService
from covalent.application.services.skill_service import (
    _reconcile_skill_process_manager,
    _sync_registry_skill_states,
)
from covalent.application.services.user_service import _seed_initial_admin_user
from covalent.infra.config_store import ConfigStore
from covalent.infra.db import DatabaseManager
from covalent.infra.delegate_repository import PostgresDelegateRunStore
from covalent.infra.memory import PersistentSessionStore
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.infra.settings import AppSettings
from covalent.runtime.backend import make_backend
from covalent.runtime.react import ReactAgentRuntime
from covalent.runtime.run_manager import RunManager
from covalent.runtime.sandbox_image_validator import DockerImageValidator

logger = logging.getLogger(__name__)


async def _application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    """Map application-layer errors to HTTP responses."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


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
    # Schema migrations are run by the explicit `migrate` command (main.py) or a deploy job —
    # not in the web lifespan — to avoid multi-replica startup races.
    db_manager = DatabaseManager(database_url, schema=settings.database_schema)
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

    # Sandbox binding resolution: every master/delegate run resolves its logical
    # (execution scope, agent) sandbox through this service. The image validator
    # port is attached later (Docker-backed validation adapter); without it,
    # profile CRUD works but validation reports unavailable.
    sandbox_repository = SandboxRepository(db_manager.session_factory)
    sandbox_profile_service = SandboxProfileService(
        sandbox_repository, settings, image_validator=DockerImageValidator(settings)
    )

    def _skill_runtime_lookup(skill_name: str) -> str | None:
        spec = registry.manifest_skills.get(skill_name)
        return spec.runtime.type if spec is not None and spec.runtime is not None else None

    sandbox_binding_service = SandboxBindingService(
        repository=sandbox_repository,
        profile_service=sandbox_profile_service,
        settings=settings,
        execution_backend=execution_backend,
        skill_runtime_lookup=_skill_runtime_lookup,
        process_manager=getattr(registry, "skill_process_manager", None),
    )
    # Emergency revocation: disabling a profile stops its live instances.
    sandbox_profile_service._on_profile_disabled = (  # noqa: SLF001 — wiring seam
        sandbox_binding_service.stop_instances_for_profile
    )
    # First boot with an empty profile table seeds the compatibility default
    # from the deployment's Docker settings (legacy_unverified, executable).
    try:
        await sandbox_profile_service.ensure_seeded()
    except Exception:
        logger.warning("Failed to seed the default sandbox profile", exc_info=True)

    app.state.settings = settings
    app.state.execution_backend = execution_backend
    app.state.db_manager = db_manager
    app.state.config_store = config_store
    app.state.registry = registry
    app.state.skill_loader = loader
    app.state.session_store = PersistentSessionStore(db_manager.session_factory)
    app.state.api_token_run_limiter = _ApiTokenRunLimiter(settings.api_token_max_concurrent_runs)
    app.state.sandbox_binding_service = sandbox_binding_service
    app.state.sandbox_profile_service = sandbox_profile_service

    # Reclaim sandbox containers orphaned by a previous run, then start a periodic
    # reaper that stops idle instances and removes containers whose session (or
    # run scope) no longer exists. No-op for the FileSystem backend.
    await execution_backend.startup_sweep()
    reaper_task = asyncio.create_task(
        _sandbox_reaper_loop(
            execution_backend,
            app.state.session_store,
            settings.execution_backend_docker_reaper_interval_seconds,
            settings.execution_backend_docker_idle_timeout_seconds,
            process_manager=getattr(registry, "skill_process_manager", None),
            sandbox_repository=sandbox_repository,
        )
    )

    # Stateful delegates (flag-gated): construct the delegate run store and
    # service, hand the service to the runtime as its delegate coordinator,
    # and only THEN register the delegate tools — the marker-protocol tools
    # (delegate_send returns a raw handle) must never exist on a runtime
    # without a coordinator, or a mis-wired assembly would leak handles into
    # model-visible tool results. Flag off: nothing is constructed and the
    # runtime keeps its default no-coordinator behavior.
    delegate_service: DelegateService | None = None
    if settings.stateful_delegates_enabled:
        delegate_run_store = PostgresDelegateRunStore(db_manager.session_factory)
        delegate_service = DelegateService(
            registry=registry, run_store=delegate_run_store, settings=settings
        )
        app.state.delegate_service = delegate_service
        app.state.delegate_run_store = delegate_run_store

    app.state.runtime = ReactAgentRuntime(
        registry,
        session_store=app.state.session_store,
        session_history_limit=settings.session_history_limit,
        context_compact_threshold=settings.context_compact_threshold,
        context_summary_model=settings.context_summary_model,
        enable_llm_summarization=settings.enable_llm_summarization,
        binding_resolver=sandbox_binding_service,
        delegate_coordinator=delegate_service,
    )
    if delegate_service is not None:
        register_ask_parent_tool(registry)
        register_delegate_lifecycle_tools(registry, delegate_service)
        # Reconcile runs orphaned by a previous process crash and TTL-expired
        # rows before the first request. Failure logs and never blocks startup.
        try:
            counts = await run_startup_sweeps(delegate_service)
            if counts["recovered"] or counts["expired"]:
                logger.info(
                    "Delegate startup sweeps: %s runs recovered, %s expired",
                    counts["recovered"],
                    counts["expired"],
                )
        except Exception:
            logger.warning("Delegate startup sweeps failed", exc_info=True)
    else:
        # Legacy coordinator-less delegates still get the forward tool: the
        # runtime resolves the text from the transcript (see _run_stream).
        register_local_answer_from_delegate_tool(registry)
    app.state.agent_invocation = AgentInvocationService(registry, app.state.runtime)

    # Durable chat runs: background execution decoupled from SSE connections.
    run_manager = RunManager(db_manager.session_factory)
    app.state.run_manager = run_manager
    try:
        await run_manager.sweep_orphans()
    except Exception:
        logger.warning("Chat run orphan sweep failed", exc_info=True)

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
    app = FastAPI(
        title="Covalent",
        version="0.3.0",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Auth"},
            {"name": "Users"},
            {"name": "API Tokens"},
            {"name": "Agents"},
            {"name": "Sessions"},
            {"name": "Providers"},
            {"name": "MCP"},
            {"name": "Skills"},
            {"name": "Sandbox Profiles"},
            {"name": "Config"},
            {"name": "Ops"},
            {"name": "Public"},
        ],
    )
    app.add_middleware(ConsoleAuthGuardMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(ApplicationError, _application_error_handler)

    from covalent.api.routes import agents, auth, config, mcp, ops, providers, public, sandbox_profiles, sessions, skills, tokens, users
    app.include_router(ops.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(tokens.router)
    app.include_router(agents.router)
    app.include_router(sessions.router)
    app.include_router(providers.router)
    app.include_router(config.router)
    app.include_router(mcp.router)
    app.include_router(skills.router)
    app.include_router(public.router)
    app.include_router(sandbox_profiles.router)

    return app
