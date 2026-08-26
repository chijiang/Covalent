"""agents route group."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from datetime import UTC
from datetime import datetime
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
import json

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _new_chat_item_id
from covalent.api._shared import _payload_text
from covalent.api._shared import to_agent_summary
from covalent.application.schemas import AgentRunRequest
from covalent.application.schemas import AgentRunResponse
from covalent.application.schemas import AgentSummaryResponse
from covalent.application.schemas import LocalToolSummaryResponse
from covalent.api.sse_events import SSE_EVENT_ASSISTANT
from covalent.api.sse_events import SSE_EVENT_ASSISTANT_DELTA
from covalent.api.sse_events import SSE_EVENT_DELEGATE_TOOL_RESULTS
from covalent.api.sse_events import SSE_EVENT_ERROR
from covalent.api.sse_events import SSE_EVENT_FINAL
from covalent.api.sse_events import SSE_EVENT_INPUT_RESOLVED
from covalent.api.sse_events import SSE_EVENT_SESSION
from covalent.api.sse_events import SSE_EVENT_TOOL_RESULTS
from covalent.api.sse_events import TRACE_ACTIVITY_EVENTS
from covalent.application.services.management_service import _available_local_tool_summaries
from covalent.application.services.management_service import _ensure_console_principal_can_access_agent
from covalent.application.services.management_service import _ensure_console_principal_can_access_session
from covalent.application.services.management_service import _resolve_console_agent_name
from covalent.application.services.session_service import AgentRunInput
from covalent.application.services.session_service import _append_assistant_attachments
from covalent.application.services.session_service import _build_resume_tool_result
from covalent.application.services.session_service import _build_session_preview
from covalent.application.services.session_service import _build_user_transcript_message
from covalent.application.services.session_service import _extract_pending_user_input
from covalent.application.services.session_service import _generate_session_title
from covalent.application.services.session_service import _payload_output_text
from covalent.application.services.session_service import _published_download_attachments_from_tool_results
from covalent.application.services.session_service import _replace_assistant_transcript
from covalent.application.services.session_service import _upsert_assistant_transcript
from covalent.infra.config_store import ConfigStore
from covalent.infra.db import DatabaseManager
from covalent.infra.memory import ChatActivityItem
from covalent.infra.memory import ChatSessionRecord
from covalent.infra.memory import SessionStore
from covalent.infra.settings import AppSettings
from covalent.model.base import ModelProviderError
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.run_manager import RUN_STATUS_COMPLETED
from covalent.runtime.run_manager import RUN_STATUS_FAILED
from covalent.runtime.run_manager import RunManager

router = APIRouter(tags=["Agents"])


def _agent_run_input(run_request):
    return AgentRunInput(input=run_request.input, metadata=dict(run_request.metadata or {}))

logger = logging.getLogger(__name__)


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, str]]:
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    agents = await config_store.get_document("agents", principal.config)
    return [
        {
            "name": str(agent.get("name") or ""),
            "internal_name": str(agent.get("internal_name") or agent.get("name") or ""),
            "description": str(agent.get("description") or ""),
        }
        for agent in agents
        if str(agent.get("name") or "") and agent.get("enabled", True) is not False
    ]

@router.get("/local-tools")
async def list_local_tools(request: Request) -> list[LocalToolSummaryResponse]:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    await _resolve_console_principal(request, db_manager)
    return _available_local_tool_summaries(registry, settings)

@router.get("/agents/{agent_name}")
async def get_agent(request: Request, agent_name: str) -> AgentSummaryResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    registry: FrameworkRegistry = request.app.state.registry
    principal = await _resolve_console_principal(request, db_manager)
    resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
    await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
    try:
        summary = to_agent_summary(registry.get_agent(resolved_agent_name))
        return summary.model_copy(update={"name": agent_name})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

@router.post("/agents/{agent_name}/run")
async def run_agent(request: Request, agent_name: str, run_request: AgentRunRequest) -> AgentRunResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    registry: FrameworkRegistry = request.app.state.registry
    service = request.app.state.agent_invocation
    principal = await _resolve_console_principal(request, db_manager)
    resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
    await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
    session_id = run_request.session_id or _new_chat_item_id("session")
    try:
        agent = registry.get_agent(resolved_agent_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

    try:
        result = await service.run(
            resolved_agent_name, run_request.input, session_id, run_request.metadata,
            getattr(request.app.state, "execution_backend", None),
            principal.workspace_id,
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



async def _prepare_agent_run(
    request: Request, agent_name: str, run_request: AgentRunRequest
):
    """Shared console-run validation: principal, agent access, session access,
    and pending-input resume resolution."""
    db_manager: DatabaseManager = request.app.state.db_manager
    registry: FrameworkRegistry = request.app.state.registry
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
    await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
    session_id = run_request.session_id or _new_chat_item_id("session")
    try:
        agent = registry.get_agent(resolved_agent_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}") from exc

    existing = await session_store.get_session(session_id)
    if existing is not None:
        _ensure_console_principal_can_access_session(principal, existing)
    pending_input = _extract_pending_user_input(existing.activity) if existing else None
    resume_tool_result = _build_resume_tool_result(_agent_run_input(run_request), pending_input)
    if pending_input is not None and resume_tool_result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session '{session_id}' is waiting for an answer to '{pending_input.title}'. "
                "Submit the pending question response before sending a new message."
            ),
        )
    return principal, agent, session_id, existing, resume_tool_result


async def _ensure_session_row(
    session_store: SessionStore, principal, agent, session_id: str, existing
) -> None:
    """The chat_runs FK requires the session row to exist before the run is
    created; pre-create an empty record for brand-new sessions."""
    if existing is not None:
        return
    now = datetime.now(UTC)
    await session_store.save_session(
        ChatSessionRecord(
            id=session_id,
            title="New conversation",
            title_source="auto",
            agent_name=agent.name,
            owner_user_id=principal.user_id,
            workspace_id=principal.workspace_id,
            preview_text="",
            created_at=now,
            updated_at=now,
        )
    )


def _build_chat_run_worker(
    *,
    manager: RunManager,
    run_id: str,
    service,
    registry: FrameworkRegistry,
    session_store: SessionStore,
    resolved_agent_name: str,
    agent,
    run_request: AgentRunRequest,
    session_id: str,
    existing,
    resume_tool_result,
    principal,
    execution_backend,
):
    """Background worker driving one chat turn: the former SSE generator logic,
    writing events into the run log instead of yielding them over a connection."""

    async def worker() -> None:
        transcript_messages = [message.model_copy(deep=True) for message in existing.messages] if existing else []
        activity = [item.model_copy(deep=True) for item in existing.activity] if existing else []
        deltas_streamed = False
        user_transcript = _build_user_transcript_message(_agent_run_input(run_request))
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

        final_status: str | None = RUN_STATUS_COMPLETED
        # The run's event log is self-contained: reattach clients rebuild the
        # turn's user bubble from this event (the transcript row is only
        # persisted when the run finishes).
        run_metadata = dict(run_request.metadata or {})
        await manager.append_event(
            run_id,
            "run_started",
            {
                "display_input": str(run_metadata.get("display_input") or run_request.input or ""),
                "user_message_id": str(run_metadata.get("user_message_id") or ""),
            },
        )
        try:
            async for event in service.stream(
                resolved_agent_name, run_request.input, session_id, runtime_metadata,
                execution_backend,
                principal.workspace_id,
            ):
                event_name = event["event"]
                payload = event["payload"]
                if event_name == SSE_EVENT_ASSISTANT_DELTA:
                    text = _payload_text(payload)
                    if text:
                        deltas_streamed = True
                        _upsert_assistant_transcript(transcript_messages, assistant_message_id, text)
                elif event_name == SSE_EVENT_ASSISTANT:
                    text = _payload_text(payload)
                    # Deltas already carried this iteration's text fragment by
                    # fragment; upserting again would duplicate it.
                    if text and not deltas_streamed:
                        _upsert_assistant_transcript(transcript_messages, assistant_message_id, text)
                    deltas_streamed = False
                elif event_name == SSE_EVENT_FINAL:
                    deltas_streamed = False
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

                await manager.append_event(run_id, event_name, payload)
        except asyncio.CancelledError:
            # Partial transcript/activity still flush below in the finally
            # block; the run manager records the terminal cancelled event.
            final_status = None
            raise
        except ModelProviderError as exc:
            status_code = 502 if exc.status_code is None else min(max(exc.status_code, 400), 599)
            payload = {"status_code": status_code, "detail": exc.detail}
            activity.append(ChatActivityItem(id=_new_chat_item_id(SSE_EVENT_ERROR), title=SSE_EVENT_ERROR, payload=payload))
            await manager.append_event(run_id, SSE_EVENT_ERROR, payload)
            final_status = RUN_STATUS_FAILED
        except Exception as exc:
            logger.exception("Agent run failed", extra={"agent_name": resolved_agent_name, "session_id": session_id})
            payload = {"status_code": 500, "detail": str(exc) or "Agent run failed unexpectedly."}
            activity.append(ChatActivityItem(id=_new_chat_item_id(SSE_EVENT_ERROR), title=SSE_EVENT_ERROR, payload=payload))
            await manager.append_event(run_id, SSE_EVENT_ERROR, payload)
            final_status = RUN_STATUS_FAILED
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
            await manager.append_event(
                run_id,
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
            if final_status is not None:
                await manager.finish_run(run_id, final_status)

    return worker


async def _launch_agent_run(
    request: Request, agent_name: str, run_request: AgentRunRequest
) -> tuple[str, str]:
    """Validate, create the run row, and spawn the background worker. Returns
    ``(run_id, session_id)``."""
    principal, agent, session_id, existing, resume_tool_result = await _prepare_agent_run(request, agent_name, run_request)
    manager: RunManager = request.app.state.run_manager
    session_store: SessionStore = request.app.state.session_store

    open_run = await manager.open_run_for_session(session_id)
    if open_run is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session '{session_id}' already has a running agent turn ({open_run.id}). "
                "Cancel it or wait for it to finish before sending a new message."
            ),
        )

    await _ensure_session_row(session_store, principal, agent, session_id, existing)
    run_id = _new_chat_item_id("run")
    await manager.create_run(
        run_id=run_id,
        session_id=session_id,
        agent_name=agent.name,
        owner_user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        input_json={
            "input": run_request.input,
            "metadata": dict(run_request.metadata or {}),
        },
    )
    worker = _build_chat_run_worker(
        manager=manager,
        run_id=run_id,
        service=request.app.state.agent_invocation,
        registry=request.app.state.registry,
        session_store=session_store,
        resolved_agent_name=agent.name,
        agent=agent,
        run_request=run_request,
        session_id=session_id,
        existing=existing,
        resume_tool_result=resume_tool_result,
        principal=principal,
        execution_backend=getattr(request.app.state, "execution_backend", None),
    )
    manager.start_run(run_id, worker)
    return run_id, session_id


def _encode_run_sse(position: int, event_name: str, payload: dict) -> str:
    return (
        f"event: {event_name}\n"
        f"id: {position}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _run_events_response(request: Request, run_id: str, after: int):
    manager: RunManager = request.app.state.run_manager
    run = await manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if run.owner_user_id is not None and run.owner_user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this run.")

    async def event_stream():
        async for position, event_name, payload in manager.stream_events(run_id, after):
            yield _encode_run_sse(position, event_name, payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_sse_headers())


@router.post("/agents/{agent_name}/runs")
async def start_agent_run(request: Request, agent_name: str, run_request: AgentRunRequest) -> dict[str, str]:
    run_id, session_id = await _launch_agent_run(request, agent_name, run_request)
    return {"run_id": run_id, "session_id": session_id}


@router.get("/agents/{agent_name}/runs")
async def list_agent_runs(request: Request, agent_name: str, session_id: str) -> list[dict[str, object]]:
    manager: RunManager = request.app.state.run_manager
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    runs = await manager.list_runs_for_session(session_id)
    visible = [run for run in runs if run.owner_user_id is None or run.owner_user_id == principal.user_id]
    return [
        {
            "id": run.id,
            "session_id": run.session_id,
            "agent_name": run.agent_name,
            "status": run.status,
            "created_at": run.created_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for run in visible
    ]


@router.get("/agents/{agent_name}/runs/{run_id}/events")
async def stream_agent_run_events(
    request: Request, agent_name: str, run_id: str, after: int = 0
):
    # EventSource-compatible reconnect: the Last-Event-ID header wins when the
    # query param is absent (0).
    last_event_id = request.headers.get("last-event-id")
    effective_after = after if after > 0 else int(last_event_id or 0)
    return await _run_events_response(request, run_id, effective_after)


@router.post("/agents/{agent_name}/runs/{run_id}/cancel")
async def cancel_agent_run(request: Request, agent_name: str, run_id: str) -> dict[str, str]:
    manager: RunManager = request.app.state.run_manager
    run = await manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    if run.owner_user_id is not None and run.owner_user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this run.")
    result = await manager.cancel_run(run_id)
    if result == "unknown":  # pragma: no cover - checked above
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return {"status": result}


@router.post("/agents/{agent_name}/stream")
async def stream_agent(request: Request, agent_name: str, run_request: AgentRunRequest) -> StreamingResponse:
    # Compatibility endpoint: creates a background run and immediately opens
    # an SSE view over it. A client disconnect no longer kills the run.
    run_id, _ = await _launch_agent_run(request, agent_name, run_request)
    return await _run_events_response(request, run_id, 0)
