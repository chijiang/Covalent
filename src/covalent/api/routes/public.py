"""public route group."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from datetime import UTC
from datetime import datetime
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from time import perf_counter
from typing import Any

from covalent.api._auth_helpers import _request_metadata
from covalent.api._shared import _new_chat_item_id
from covalent.api.auth import authenticate_api_token
from covalent.api.auth import require_agent_allowed
from covalent.api.auth import require_memory_mode_allowed
from covalent.api.auth import require_scope
from covalent.api.auth import require_trace_level_allowed
from covalent.application.errors import ApplicationError
from covalent.application.schemas import PublicAgentInvokeRequest
from covalent.application.schemas import PublicAgentInvokeResponse
from covalent.application.services.invoke_service import _ApiTokenRunLimiter
from covalent.application.services.invoke_service import _encode_public_sse
from covalent.application.services.invoke_service import _enforce_api_token_policy_limits
from covalent.application.services.invoke_service import _public_run_completed_payload
from covalent.application.services.invoke_service import _public_stream_events
from covalent.application.services.invoke_service import _record_denied_public_agent_invoke
from covalent.application.services.invoke_service import _record_public_agent_run
from covalent.application.services.invoke_service import _resolve_public_invoke_session_id
from covalent.application.services.invoke_service import _usage_payload
from covalent.application.services.management_service import _ensure_api_principal_can_invoke_agent
from covalent.application.services.management_service import _resolve_api_agent_name
from covalent.core.types import RunContext
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings
from covalent.model.base import ModelProviderError
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.react import ReactAgentRuntime

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/v1/agent/invoke", response_model=None)
async def public_invoke_agent(request: Request, invoke_request: PublicAgentInvokeRequest) -> PublicAgentInvokeResponse | StreamingResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    registry: FrameworkRegistry = request.app.state.registry
    runtime: ReactAgentRuntime = request.app.state.runtime

    memory_mode = invoke_request.memory.mode
    trace_level = invoke_request.trace.level
    try:
        principal = await authenticate_api_token(
            request,
            settings=settings,
            session_factory=db_manager.session_factory,
        )
    except (HTTPException, ApplicationError) as exc:
        await _record_denied_public_agent_invoke(
            db_manager,
            principal=None,
            agent_name=invoke_request.agent,
            memory_mode=memory_mode,
            request_metadata=_request_metadata(request),
            reason=exc.message if isinstance(exc, ApplicationError) else str(exc.detail),
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
    except (HTTPException, ApplicationError) as exc:
        await _record_denied_public_agent_invoke(
            db_manager,
            principal=principal,
            agent_name=resolved_agent_name,
            memory_mode=memory_mode,
            request_metadata=_request_metadata(request),
            reason=exc.message if isinstance(exc, ApplicationError) else str(exc.detail),
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
    # Execution identity: a persistent chat run's scope is its session; a
    # stateless run's scope is the run id (with NO session_id — never a fake
    # chat session). The binding resolver assigns per-agent sandbox instances
    # from this scope at run start; container creation stays lazy.
    context = RunContext(
        agent_name=agent.name,
        session_id=session_id,
        execution_scope_id=session_id or run_id,
        workspace_scope_id=session_id or run_id,
        workspace_id=principal.workspace_id,
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
        execution_backend=getattr(request.app.state, "execution_backend", None),
    )

    async def _cleanup_stateless_scope() -> None:
        """Full run-scope teardown for memory.mode=none. Cancellation-safe; runs
        on success, failure, timeout, and client disconnect."""
        if memory_mode != "none":
            return
        binding_service = getattr(request.app.state, "sandbox_binding_service", None)
        if binding_service is None:
            return
        try:
            await binding_service.cleanup_stateless_run(run_id)
        except Exception:
            logger.warning(
                "Stateless run scope cleanup failed for %s", run_id, exc_info=True
            )

    if invoke_request.stream:
        limiter: _ApiTokenRunLimiter | None = getattr(request.app.state, "api_token_run_limiter", None)

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
                try:
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
                finally:
                    # Resource cleanup must run even if run-log recording fails,
                    # otherwise a stateless sandbox + its concurrency slot leak.
                    await _cleanup_stateless_scope()
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

    limiter: _ApiTokenRunLimiter | None = getattr(request.app.state, "api_token_run_limiter", None)
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
        await _cleanup_stateless_scope()
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
