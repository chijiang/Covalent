"""Shared leaf-layer helpers for the API package.

Extracted from ``app.py`` so that route modules and the app factory can share
small utilities, response mappers, and cross-cutting helpers without circular
imports. This module must stay dependency-free within the ``api`` package
(no imports of sibling helper modules).

Imports into ``app.py`` from here must not create cycles: ``_shared`` only
imports from stdlib, ``covalent.infra``, and ``covalent.core``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

import anyio
from fastapi import Request

from covalent.application._utils import (
    _coerce_int,  # noqa: F401  (re-exported for API-layer callers)
    _new_chat_item_id,
    _payload_text,  # noqa: F401  (re-exported for API-layer callers)
    _safe_storage_component,  # noqa: F401  (re-exported for API-layer callers)
)
from covalent.application.principal import Principal as ConsolePrincipalContext

from covalent.api.auth import ApiPrincipal
from covalent.application.schemas import (
    AgentRunLogResponse,
    AgentSummaryResponse,
    ApiTokenSummaryResponse,
    AuditLogResponse,
    ChatSessionActivityResponse,
    ChatSessionMessageResponse,
    ChatSessionResponse,
    ChatSessionSummaryResponse,
    ProviderSummaryResponse,
)
from covalent.core.agent import AgentSpec
from covalent.infra.db import (
    AgentRunLogRow,
    ApiTokenRow,
    AuditLogRow,
    DatabaseManager,
    UserRow,
    WorkspaceRow,
)
from covalent.infra.memory import ChatSessionRecord, ChatSessionSummary, SessionStore
from covalent.runtime.backend import ExecutionBackend

logger = logging.getLogger(__name__)



















def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    for info in archive.infolist():
        if info.is_dir():
            continue
        extracted = (target_dir / info.filename).resolve()
        if not extracted.is_relative_to(target_dir):
            raise ValueError(f"Zip entry escapes target directory: {info.filename}")
    archive.extractall(target_dir)



async def _rmtree_async(path: Path, *, ignore_errors: bool = False) -> None:
    """shutil.rmtree off the event loop. Large or network-attached session
    workspaces would otherwise block the loop for the full duration of the walk.
    """
    await anyio.to_thread.run_sync(
        functools.partial(shutil.rmtree, path, ignore_errors=ignore_errors)
    )






def _audit_request_metadata(request: Request | None) -> dict[str, str | None]:
    if request is None:
        return {"request_id": None, "ip_address": None, "user_agent": None}
    forwarded_for = request.headers.get("x-forwarded-for")
    client_host = request.client.host if request.client else None
    return {
        "request_id": request.headers.get("x-request-id") or request.headers.get("x-correlation-id"),
        "ip_address": (forwarded_for.split(",", 1)[0].strip() if forwarded_for else client_host),
        "user_agent": request.headers.get("user-agent"),
    }



async def _record_audit_log(
    db_manager: DatabaseManager,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    outcome: str = "success",
    principal: ConsolePrincipalContext | None = None,
    api_principal: ApiPrincipal | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    request_metadata = _audit_request_metadata(request)
    actor_user_id = principal.user_id if principal is not None else api_principal.user_id if api_principal is not None else None
    actor_token_id = api_principal.token_id if api_principal is not None else None
    workspace_id = principal.workspace_id if principal is not None else api_principal.workspace_id if api_principal is not None else None
    async with db_manager.session_factory() as session:
        async with session.begin():
            session.add(
                AuditLogRow(
                    id=_new_chat_item_id("audit"),
                    actor_user_id=actor_user_id,
                    actor_token_id=actor_token_id,
                    workspace_id=workspace_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    outcome=outcome,
                    request_id=request_metadata["request_id"],
                    ip_address=request_metadata["ip_address"],
                    user_agent=request_metadata["user_agent"],
                    metadata_json=dict(metadata or {}),
                )
            )




async def _augment_sandbox_snapshot(snapshot: dict[str, Any], session_store: SessionStore) -> dict[str, Any]:
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        return snapshot

    async def enrich(raw_session: object) -> object:
        if not isinstance(raw_session, dict):
            return raw_session
        session = dict(raw_session)
        session_id = str(session.get("session_id") or "")
        if not session_id:
            return session
        try:
            record = await session_store.get_session(session_id)
        except Exception as exc:
            session["session_lookup_error"] = str(exc)
            return session
        if record is None:
            session["session_missing"] = True
            return session
        if not session.get("agent_name") and record.agent_name:
            session["agent_name"] = record.agent_name
        session.update(
            {
                "chat_title": record.title,
                "chat_message_count": record.message_count,
                "owner_user_id": record.owner_user_id,
                "workspace_id": record.workspace_id,
                "created_by_token_id": record.created_by_token_id,
                "session_created_at": record.created_at.isoformat(),
                "session_updated_at": record.updated_at.isoformat(),
            }
        )
        return session

    augmented = dict(snapshot)
    augmented["sessions"] = await asyncio.gather(*(enrich(session) for session in sessions))
    return augmented



def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(int(value), 0)
        if isinstance(value, str):
            try:
                return max(int(value.strip()), 0)
            except ValueError:
                continue
    return 0



def to_agent_summary(agent: AgentSpec) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        name=agent.name,
        description=agent.description,
        system_prompt=agent.system_prompt,
        reasoning_prompt=agent.reasoning_prompt,
        reasoning_level=agent.reasoning_level,
        enabled=True,
        skills=agent.skills,
        local_tools=agent.local_tools,
        allowed_outbound=getattr(agent, "allowed_outbound", []) or [],
        sandbox_profile_id=getattr(agent, "sandbox_profile_id", None),
        delegate_agents=agent.delegate_agents,
        capabilities=agent.capabilities,
        max_iterations=agent.max_iterations,
        context_window=agent.context_window,
        provider=ProviderSummaryResponse(
            model=agent.provider.model,
            timeout_seconds=agent.provider.timeout_seconds,
        ),
    )



def to_chat_session_summary_response(record: ChatSessionSummary) -> ChatSessionSummaryResponse:
    return ChatSessionSummaryResponse(**record.model_dump())



_ACTIVITY_RAW_PAYLOAD_KEYS = ("raw_request", "raw_response")


def strip_activity_payload(payload: Any) -> tuple[Any, bool, bool]:
    """Drop raw_request/raw_response blobs from an activity payload.

    Returns the stripped payload plus flags recording whether each raw blob
    was present, so list responses stay small while the frontend can still
    offer on-demand raw views via the activity detail endpoint.
    """
    if not isinstance(payload, dict):
        return payload, False, False
    has_request = "raw_request" in payload
    has_response = "raw_response" in payload
    if not (has_request or has_response):
        return payload, False, False
    stripped = {
        key: value
        for key, value in payload.items()
        if key not in _ACTIVITY_RAW_PAYLOAD_KEYS
    }
    return stripped, has_request, has_response



def to_chat_session_response(record: ChatSessionRecord) -> ChatSessionResponse:
    activity: list[ChatSessionActivityResponse] = []
    for item in record.activity:
        payload, has_raw_request, has_raw_response = strip_activity_payload(item.payload)
        # Store-level loads already stripped and flagged; union so records
        # carrying unstripped payloads (save paths) still report correctly.
        activity.append(
            ChatSessionActivityResponse(
                id=item.id,
                title=item.title,
                payload=payload,
                has_raw_request=has_raw_request or bool(getattr(item, "has_raw_request", False)),
                has_raw_response=has_raw_response or bool(getattr(item, "has_raw_response", False)),
            )
        )
    messages = record.messages
    # Paged loads set message_count to the transcript total; fall back to
    # the in-memory list length when a record never carried a count.
    messages_total = getattr(record, "message_count", 0) or len(messages)
    # Positions are dense from 0 (both stores renumber on save), so a page
    # whose first message sits above 0 has older messages behind it.
    first_position = getattr(messages[0], "position", None) if messages else None
    messages_has_more = first_position is not None and first_position > 0
    return ChatSessionResponse(
        **to_chat_session_summary_response(record).model_dump(),
        messages=[ChatSessionMessageResponse(**message.model_dump()) for message in messages],
        messages_total=messages_total,
        messages_has_more=messages_has_more,
        activity=activity,
    )



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



def _agent_run_log_response(row: AgentRunLogRow) -> AgentRunLogResponse:
    return AgentRunLogResponse(
        id=row.id,
        user_id=row.user_id,
        token_id=row.token_id,
        workspace_id=row.workspace_id,
        agent_name=row.agent_name,
        memory_mode=row.memory_mode,
        session_id=row.session_id,
        status=row.status,
        latency_ms=row.latency_ms,
        provider=row.provider,
        model=row.model,
        usage=dict(row.usage_json or {}),
        error=dict(row.error_json or {}),
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
    )



def _audit_log_response(row: AuditLogRow) -> AuditLogResponse:
    return AuditLogResponse(
        id=row.id,
        actor_user_id=row.actor_user_id,
        actor_token_id=row.actor_token_id,
        workspace_id=row.workspace_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        outcome=row.outcome,
        request_id=row.request_id,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
    )



async def _sandbox_reaper_loop(
    backend: ExecutionBackend,
    session_store: SessionStore,
    interval_seconds: float,
    idle_timeout_seconds: float = 0.0,
    process_manager: Any | None = None,
    sandbox_repository: Any | None = None,
) -> None:
    """Periodically reclaim sandbox containers, per instance.

    Tracked instances (managed by this backend process): stopped individually
    once idle beyond ``idle_timeout_seconds`` — the logical binding row is kept,
    so the next run recreates the container. Untracked labeled containers
    (orphans from a previous process): removed when their chat session is gone
    from the store, or — for run-scoped orphans with no session label — when no
    logical binding row remains for the scope. No-op for the FileSystem backend.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            summaries_fn = getattr(backend, "list_sandbox_instance_summaries", None)
            if callable(summaries_fn):
                for summary in await summaries_fn():
                    try:
                        await _reap_sandbox_instance(
                            backend,
                            session_store,
                            summary,
                            idle_timeout_seconds=idle_timeout_seconds,
                            process_manager=process_manager,
                            sandbox_repository=sandbox_repository,
                        )
                    except Exception as exc:
                        logger.error("Sandbox reaper error for instance %s: %s", summary, exc)
                continue
            # Legacy backends without instance summaries.
            for session_id in await backend.list_sandbox_sessions():
                try:
                    if not getattr(backend, "is_session_tracked", lambda _: False)(session_id):
                        if await session_store.get_session(session_id) is None:
                            logger.info("Reaping orphan sandbox container for session %s", session_id)
                            await backend.stop(session_id)
                except Exception as exc:
                    logger.error("Sandbox reaper error for session %s: %s", session_id, exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Sandbox reaper loop error: %s", exc)


async def _reap_sandbox_instance(
    backend: ExecutionBackend,
    session_store: SessionStore,
    summary: dict[str, object],
    *,
    idle_timeout_seconds: float,
    process_manager: Any | None,
    sandbox_repository: Any | None,
) -> None:
    instance_id = str(summary.get("sandbox_instance_id") or "")
    if not instance_id:
        return
    is_tracked_fn = getattr(backend, "is_instance_tracked", None)
    tracked = callable(is_tracked_fn) and is_tracked_fn(instance_id)
    if tracked:
        if idle_timeout_seconds <= 0:
            return
        idle_fn = getattr(backend, "instance_idle_seconds", None)
        idle = idle_fn(instance_id) if callable(idle_fn) else None
        if idle is None or idle < idle_timeout_seconds:
            return
        logger.info(
            "Stopping idle sandbox instance %s (idle %ds >= %ds)",
            instance_id, int(idle), int(idle_timeout_seconds),
        )
        if process_manager is not None:
            try:
                await process_manager.stop_sandbox_instance(instance_id)
            except Exception:
                logger.debug("Process eviction failed for idle instance %s", instance_id, exc_info=True)
        await backend.stop_instance(instance_id)
        return

    # Untracked: an orphan from a previous process (or another worker's
    # container). Session-scoped: reap only when the session is gone.
    session_id = summary.get("session_id")
    if session_id:
        if await session_store.get_session(str(session_id)) is None:
            logger.info("Reaping orphan sandbox container for session %s", session_id)
            await backend.stop_instance(instance_id)
        return
    # Run-scoped orphan (no session label): reap only when no logical binding
    # remains — a live run in another worker still holds its binding rows.
    scope_id = str(summary.get("execution_scope_id") or "")
    if not scope_id or sandbox_repository is None:
        return
    bindings = await sandbox_repository.list_bindings_by_scope(scope_id)
    if not bindings:
        logger.info("Reaping orphan run-scoped sandbox container for scope %s", scope_id)
        await backend.stop_instance(instance_id)

