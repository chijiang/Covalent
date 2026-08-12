"""agents route group."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from datetime import UTC
from datetime import datetime
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
import json

from agent_framework.api._auth_helpers import _resolve_console_principal
from agent_framework.api._shared import _new_chat_item_id
from agent_framework.api._shared import _payload_text
from agent_framework.api._shared import _record_sandbox_session
from agent_framework.api._shared import to_agent_summary
from agent_framework.api.schemas import AgentRunRequest
from agent_framework.api.schemas import AgentRunResponse
from agent_framework.api.schemas import AgentSummaryResponse
from agent_framework.api.schemas import LocalToolSummaryResponse
from agent_framework.api.sse_events import SSE_EVENT_ASSISTANT
from agent_framework.api.sse_events import SSE_EVENT_DELEGATE_TOOL_RESULTS
from agent_framework.api.sse_events import SSE_EVENT_ERROR
from agent_framework.api.sse_events import SSE_EVENT_FINAL
from agent_framework.api.sse_events import SSE_EVENT_INPUT_RESOLVED
from agent_framework.api.sse_events import SSE_EVENT_SESSION
from agent_framework.api.sse_events import SSE_EVENT_TOOL_RESULTS
from agent_framework.api.sse_events import TRACE_ACTIVITY_EVENTS
from agent_framework.application.services.management_service import _available_local_tool_summaries
from agent_framework.application.services.management_service import _ensure_console_principal_can_access_agent
from agent_framework.application.services.management_service import _ensure_console_principal_can_access_session
from agent_framework.application.services.management_service import _resolve_console_agent_name
from agent_framework.application.services.session_service import _append_assistant_attachments
from agent_framework.application.services.session_service import _build_resume_tool_result
from agent_framework.application.services.session_service import _build_session_preview
from agent_framework.application.services.session_service import _build_user_transcript_message
from agent_framework.application.services.session_service import _extract_pending_user_input
from agent_framework.application.services.session_service import _generate_session_title
from agent_framework.application.services.session_service import _payload_output_text
from agent_framework.application.services.session_service import _published_download_attachments_from_tool_results
from agent_framework.application.services.session_service import _replace_assistant_transcript
from agent_framework.application.services.session_service import _upsert_assistant_transcript
from agent_framework.core.types import RunContext
from agent_framework.infra.config_store import ConfigStore
from agent_framework.infra.db import DatabaseManager
from agent_framework.infra.memory import ChatActivityItem
from agent_framework.infra.memory import ChatSessionRecord
from agent_framework.infra.memory import SessionStore
from agent_framework.infra.settings import AppSettings
from agent_framework.model.base import ModelProviderError
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.react import ReactAgentRuntime

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/agents")
async def list_agents(request: Request) -> list[dict[str, str]]:
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    agents = await config_store.get_document("agents", principal.config)
    return [
        {"name": str(agent.get("name") or ""), "description": str(agent.get("description") or "")}
        for agent in agents
        if str(agent.get("name") or "") and agent.get("enabled", True) is not False
    ]

@router.get("/local-tools")
async def list_local_tools(request: Request) -> list[LocalToolSummaryResponse]:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
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
    runtime: ReactAgentRuntime = request.app.state.runtime
    principal = await _resolve_console_principal(request, db_manager)
    resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
    await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
    session_id = run_request.session_id or _new_chat_item_id("session")
    # Record sandbox metadata before the container is created.
    try:
        agent = registry.get_agent(resolved_agent_name)
        _record_sandbox_session(getattr(request.app.state, "execution_backend", None), session_id, agent)
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
            RunContext(agent_name=resolved_agent_name, session_id=session_id, metadata=run_request.metadata, execution_backend=getattr(request.app.state, "execution_backend", None)),
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

@router.post("/agents/{agent_name}/stream")
async def stream_agent(request: Request, agent_name: str, run_request: AgentRunRequest) -> StreamingResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    registry: FrameworkRegistry = request.app.state.registry
    runtime: ReactAgentRuntime = request.app.state.runtime
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    resolved_agent_name = await _resolve_console_agent_name(db_manager, principal, agent_name)
    await _ensure_console_principal_can_access_agent(db_manager, principal, resolved_agent_name)
    session_id = run_request.session_id or _new_chat_item_id("session")
    # Record sandbox metadata before the container is created.
    try:
        agent = registry.get_agent(resolved_agent_name)
        _record_sandbox_session(getattr(request.app.state, "execution_backend", None), session_id, agent)
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
                RunContext(agent_name=resolved_agent_name, session_id=session_id, metadata=runtime_metadata, execution_backend=getattr(request.app.state, "execution_backend", None)),
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
