"""Public agent-invoke support helpers.

Extracted from ``app.py``. Depends only on ``_shared``, ``auth``, and infra
layers — no sibling helper modules, so no import cycles.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from covalent.api._shared import (
    _coerce_int,
    _coerce_positive_int,
    _new_chat_item_id,
    _payload_text,
    _record_audit_log,
)
from covalent.api.auth import ApiPrincipal
from covalent.infra.db import AgentRunLogRow, AuditLogRow, ChatSessionRow, DatabaseManager

class _ApiTokenRunLimiter:
    """Per-token in-flight run cap. A bounded semaphore is created lazily per
    token_id (never per request) and reused across the token's lifetime. When
    the configured cap is 0 the limiter is a no-op (back-compat).

    Excess concurrent calls block on ``acquire`` (queue, not reject) so a
    burst from one token serializes rather than exhausting provider quota or
    sandbox containers. Intended to live on ``app.state`` so all workers in
    this process share it.
    """

    def __init__(self, max_per_token: int) -> None:
        self._max = max(0, int(max_per_token))
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._max > 0

    async def acquire(self, token_id: str) -> None:
        if self._max <= 0:
            return
        async with self._lock:
            sem = self._semaphores.get(token_id)
            if sem is None:
                sem = asyncio.Semaphore(self._max)
                self._semaphores[token_id] = sem
        # Acquire outside the manager lock so a full semaphore doesn't block
        # other tokens' lookups; callers await their own token's slot.
        await sem.acquire()

    async def release(self, token_id: str) -> None:
        if self._max <= 0:
            return
        sem = self._semaphores.get(token_id)
        if sem is not None:
            sem.release()

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
    try:
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
    except IntegrityError:
        # Concurrent invoke with the same client-chosen session_id: the other
        # request won the INSERT. Re-read in a fresh session and apply the same
        # ownership check as the existing-row branch above.
        pass

    async with db_manager.session_factory() as session:
        async with session.begin():
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                # The row vanished between the conflict and the re-read (e.g. the
                # winning request rolled back / deleted it). Let the caller retry.
                raise HTTPException(status_code=409, detail=f"Session conflict, retry: {session_id}")
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
            session.add(
                AuditLogRow(
                    id=_new_chat_item_id("audit"),
                    actor_user_id=principal.user_id,
                    actor_token_id=principal.token_id,
                    workspace_id=principal.workspace_id,
                    action="agent.invoke",
                    target_type="agent",
                    target_id=agent_name,
                    outcome=status,
                    metadata_json={
                        "run_id": run_id,
                        "memory_mode": memory_mode,
                        "session_id": session_id,
                        "latency_ms": latency_ms,
                        "provider": provider,
                        "model": model,
                        "usage": dict(usage or {}),
                        "error": dict(error or {}),
                    },
                )
            )

async def _enforce_api_token_policy_limits(
    db_manager: DatabaseManager,
    principal: ApiPrincipal,
    *,
    agent_name: str,
) -> None:
    policy = principal.policy or {}
    max_requests_per_minute = _coerce_positive_int(policy.get("max_requests_per_minute"))
    max_requests_per_day = _coerce_positive_int(policy.get("max_requests_per_day"))
    max_tokens_per_day = _coerce_positive_int(policy.get("max_tokens_per_day"))
    if max_requests_per_minute is None and max_requests_per_day is None and max_tokens_per_day is None:
        return

    now = datetime.now(UTC)
    minute_start = now - timedelta(minutes=1)
    day_start = now - timedelta(days=1)
    async with db_manager.session_factory() as session:
        if max_requests_per_minute is not None:
            minute_count = await session.scalar(
                select(func.count(AgentRunLogRow.id)).where(
                    AgentRunLogRow.token_id == principal.token_id,
                    AgentRunLogRow.created_at >= minute_start,
                )
            )
            if int(minute_count or 0) >= max_requests_per_minute:
                raise HTTPException(
                    status_code=429,
                    detail=f"API token request rate limit exceeded for agent '{agent_name}'",
                )

        daily_rows = None
        if max_requests_per_day is not None or max_tokens_per_day is not None:
            daily_rows = list(
                await session.scalars(
                    select(AgentRunLogRow).where(
                        AgentRunLogRow.token_id == principal.token_id,
                        AgentRunLogRow.created_at >= day_start,
                    )
                )
            )

        if max_requests_per_day is not None and daily_rows is not None and len(daily_rows) >= max_requests_per_day:
            raise HTTPException(
                status_code=429,
                detail=f"API token daily request quota exceeded for agent '{agent_name}'",
            )

        if max_tokens_per_day is not None and daily_rows is not None:
            used_tokens = sum(_coerce_int((row.usage_json or {}).get("total_tokens")) for row in daily_rows)
            if used_tokens >= max_tokens_per_day:
                raise HTTPException(
                    status_code=429,
                    detail=f"API token daily token quota exceeded for agent '{agent_name}'",
                )

async def _record_denied_public_agent_invoke(
    db_manager: DatabaseManager,
    *,
    principal: ApiPrincipal | None,
    agent_name: str,
    memory_mode: Literal["none", "session"],
    request: Request | None,
    reason: str,
    status_code: int,
) -> None:
    await _record_audit_log(
        db_manager,
        action="agent.invoke.denied",
        target_type="agent",
        target_id=agent_name,
        outcome="denied",
        api_principal=principal,
        request=request,
        metadata={
            "memory_mode": memory_mode,
            "reason": reason,
            "status_code": status_code,
        },
    )

