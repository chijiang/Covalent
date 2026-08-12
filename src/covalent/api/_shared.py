"""Shared leaf-layer helpers for the API package.

Extracted from ``app.py`` so that route modules and the app factory can share
small utilities, response mappers, and cross-cutting helpers without circular
imports. This module must stay dependency-free within the ``api`` package
(no imports of sibling helper modules).

Imports into ``app.py`` from here must not create cycles: ``_shared`` only
imports from stdlib, ``agent_framework.infra``, and ``agent_framework.core``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
from fastapi import Request

from agent_framework.api.auth import ApiPrincipal
from agent_framework.api.schemas import (
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
from agent_framework.core.agent import AgentSpec
from agent_framework.infra.config_store import ConfigPrincipal
from agent_framework.infra.db import (
    AgentRunLogRow,
    ApiTokenRow,
    AuditLogRow,
    DatabaseManager,
    UserRow,
    WorkspaceRow,
)
from agent_framework.infra.memory import ChatSessionRecord, ChatSessionSummary, SessionStore
from agent_framework.runtime.backend import ExecutionBackend

logger = logging.getLogger(__name__)

_SAFE_STORAGE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")

RESOURCE_METADATA_FIELDS = (
    "owner_user_id",
    "workspace_id",
    "visibility",
    "publication_status",
    "publication_requested_at",
    "publication_reviewed_at",
    "publication_reviewed_by_user_id",
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



def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None



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



def _safe_storage_component(raw: str, default: str) -> str:
    normalized = _SAFE_STORAGE_COMPONENT_RE.sub("-", raw.strip()).strip("._-")
    return normalized or default



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



def _payload_text(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("text")
        return "" if value is None else str(value)
    return ""



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



def _record_sandbox_session(backend: ExecutionBackend | None, session_id: str, agent: AgentSpec) -> None:
    if backend is None or not session_id:
        return
    record_fn = getattr(backend, "record_session", None)
    if not callable(record_fn):
        return
    outbound = list(getattr(agent, "allowed_outbound", None) or [])
    try:
        record_fn(session_id, agent.name, outbound)
    except Exception as exc:
        logger.debug("Failed to record sandbox metadata for session %s: %s", session_id, exc)



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
        delegate_agents=agent.delegate_agents,
        capabilities=agent.capabilities,
        max_iterations=agent.max_iterations,
        provider=ProviderSummaryResponse(
            model=agent.provider.model,
            timeout_seconds=agent.provider.timeout_seconds,
        ),
    )



def to_chat_session_summary_response(record: ChatSessionSummary) -> ChatSessionSummaryResponse:
    return ChatSessionSummaryResponse(**record.model_dump())



def to_chat_session_response(record: ChatSessionRecord) -> ChatSessionResponse:
    return ChatSessionResponse(
        **to_chat_session_summary_response(record).model_dump(),
        messages=[ChatSessionMessageResponse(**message.model_dump()) for message in record.messages],
        activity=[ChatSessionActivityResponse(**item.model_dump()) for item in record.activity],
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



@dataclass(frozen=True)
class ConsolePrincipalContext:
    user_id: str
    email: str
    display_name: str
    role: str
    workspace_id: str
    workspace_name: str
    workspace_slug: str
    workspace_role: str
    username: str | None = None
    avatar_url: str | None = None
    preferences: dict[str, Any] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def config(self) -> ConfigPrincipal:
        return ConfigPrincipal(user_id=self.user_id, workspace_id=self.workspace_id, role=self.role)



async def _sandbox_reaper_loop(
    backend: ExecutionBackend,
    session_store: SessionStore,
    interval_seconds: float,
    idle_timeout_seconds: float = 0.0,
) -> None:
    """Periodically reclaim sandbox containers.

    For sessions **actively tracked** by the backend: stop them if idle beyond
    ``idle_timeout_seconds`` (0 = never auto-stop). For sessions **not tracked**
    (orphans from a previous process): stop if the session is gone from the store.
    No-op for the FileSystem backend (``list_sandbox_sessions`` returns ``[]``).
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            for session_id in await backend.list_sandbox_sessions():
                try:
                    is_tracked = getattr(backend, "is_session_tracked", lambda _: False)(session_id)
                    if is_tracked:
                        # Active session — only stop if idle beyond the timeout.
                        if idle_timeout_seconds > 0:
                            idle = getattr(backend, "session_idle_seconds", lambda _: None)(session_id)
                            if idle is not None and idle >= idle_timeout_seconds:
                                logger.info(
                                    "Stopping idle sandbox for session %s (idle %ds >= %ds)",
                                    session_id, int(idle), int(idle_timeout_seconds),
                                )
                                await backend.stop(session_id)
                        continue
                    # Untracked (orphan from a previous run) — reap if session is gone.
                    if await session_store.get_session(session_id) is None:
                        logger.info("Reaping orphan sandbox container for session %s", session_id)
                        await backend.stop(session_id)
                except Exception as exc:
                    logger.error("Sandbox reaper error for session %s: %s", session_id, exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Sandbox reaper loop error: %s", exc)

