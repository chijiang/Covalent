"""Public agent-invoke support helpers.

Extracted from ``app.py``. Depends only on ``_shared``, ``auth``, and infra
layers — no sibling helper modules, so no import cycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Literal

import anyio

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from covalent.application._utils import _coerce_int, _coerce_positive_int, _new_chat_item_id, _payload_text
from covalent.application.audit import RequestMetadata, record_audit
from covalent.application.errors import ConflictError, InvalidInputError, NotFoundError, QuotaExceededError
from covalent.application.principal import ApiPrincipal
from covalent.infra.db import AgentRunLogRow, AuditLogRow, ChatSessionRow, DatabaseManager
from covalent.model.base import ModelProviderError

logger = logging.getLogger(__name__)


class _ApiTokenRunLimiter:
    """Per-token in-flight run cap. A bounded semaphore is created lazily per
    token_id (never per request) and reused across the token's lifetime. When
    the configured cap is 0 the limiter is a no-op (back-compat).

    Excess concurrent calls QUEUE on ``acquire`` (serialize, never reject) so
    a burst from one token is paced rather than exhausting provider quota or
    sandbox containers. The cap is per PROCESS: the limiter lives on
    ``app.state``, so multiple API workers each enforce the cap independently
    and the effective global concurrency for a token is
    ``max_concurrent_runs * worker_count``. Callers that need a hard global
    rejection at the limit should negotiate an explicit policy instead.
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
            raise InvalidInputError("session_id is only allowed when memory.mode is 'session'")
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
                    raise NotFoundError(f"Unknown session: {session_id}")
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
                raise ConflictError(f"Session conflict, retry: {session_id}")
            if row.owner_user_id != principal.user_id or row.workspace_id != principal.workspace_id:
                raise NotFoundError(f"Unknown session: {session_id}")
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
    assistant_message = final_payload.get("assistant_message")
    raw_reasoning = assistant_message.get("reasoning_content") if isinstance(assistant_message, dict) else None
    return {
        "run_id": run_id,
        "agent": agent_name,
        "memory_mode": memory_mode,
        "session_id": session_id,
        "output_text": str(final_payload.get("output_text") or ""),
        "reasoning_content": raw_reasoning.strip() if isinstance(raw_reasoning, str) and raw_reasoning.strip() else None,
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

    # 思考内容不受 trace_level 门控：app 侧依赖它流式展示模型推理过程。
    if event_name == "reasoning_delta":
        text = _payload_text(payload)
        return [_encode_public_sse("message.reasoning_delta", {"text": text})] if text else []

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

async def public_invoke_stream(
    *,
    events: AsyncIterator[dict[str, Any]],
    run_id: str,
    agent_name: str,
    memory_mode: str,
    session_id: str | None,
    created_at_iso: str,
    trace_level: str,
    limiter: _ApiTokenRunLimiter | None,
    token_id: str,
    record_run: Callable[[dict[str, Any]], Awaitable[None]],
) -> AsyncIterator[str]:
    """SSE generator for a streaming public invoke.

    This generator owns the stream lifecycle: the per-token slot is acquired
    at the top and the try/finally spans the FIRST yield (``run.created``)
    onward, so a client that disconnects immediately after the first event
    still releases the slot and runs finalization — GeneratorExit is raised at
    the suspended yield, inside the try. ``record_run`` receives the run
    summary (status / final payload / error payload / latency) and runs
    shielded before the slot is released; the caller's callback sequences
    run-log recording before scope cleanup. The terminal ``run.completed`` is
    yielded only on normal exhaustion (after finalization) — never during
    GeneratorExit unwinding.
    """
    if limiter is not None:
        await limiter.acquire(token_id)
    started = perf_counter()
    final_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] = {}
    status = "completed"
    try:
        yield _encode_public_sse(
            "run.created",
            {
                "run_id": run_id,
                "agent": agent_name,
                "memory_mode": memory_mode,
                "session_id": session_id,
                "created_at": created_at_iso,
            },
        )
        async for event in events:
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
        logger.exception(
            "Public agent invoke stream failed", extra={"agent_name": agent_name, "run_id": run_id}
        )
        status = "failed"
        error_payload = {"code": "internal_error", "message": str(exc) or "Agent run failed unexpectedly."}
        yield _encode_public_sse("run.failed", {"run_id": run_id, "error": error_payload})
    finally:
        latency_ms = int((perf_counter() - started) * 1000)
        # A client disconnect surfaces as GeneratorExit/CancelledError — a
        # BaseException, so the except clauses above never run. A run that
        # ended without producing a final answer must not be recorded as a
        # success (it would inflate success-rate and audit as completed); a
        # disconnect AFTER the final answer was generated stays completed.
        if status == "completed" and final_payload is None and sys.exc_info()[0] is not None:
            status = "aborted"
        # Shielded from client-disconnect cancellation: a cancelled yield or
        # await must not leak the run record or the concurrency slot.
        # (CancelScope is a sync context manager; the awaits inside it are
        # what the shield protects.)
        with anyio.CancelScope(shield=True):
            try:
                await record_run(
                    {
                        "status": status,
                        "final_payload": final_payload,
                        "error_payload": error_payload,
                        "latency_ms": latency_ms,
                    }
                )
            finally:
                if limiter is not None:
                    await limiter.release(token_id)
    if final_payload is not None:
        yield _encode_public_sse(
            "run.completed",
            _public_run_completed_payload(
                run_id=run_id,
                agent_name=agent_name,
                memory_mode=memory_mode,
                session_id=session_id,
                final_payload=final_payload,
            ),
        )


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
                raise QuotaExceededError(f"API token request rate limit exceeded for agent '{agent_name}'")

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
            raise QuotaExceededError(f"API token daily request quota exceeded for agent '{agent_name}'")

        if max_tokens_per_day is not None and daily_rows is not None:
            used_tokens = sum(_coerce_int((row.usage_json or {}).get("total_tokens")) for row in daily_rows)
            if used_tokens >= max_tokens_per_day:
                raise QuotaExceededError(f"API token daily token quota exceeded for agent '{agent_name}'")

async def _record_denied_public_agent_invoke(
    db_manager: DatabaseManager,
    *,
    principal: ApiPrincipal | None,
    agent_name: str,
    memory_mode: Literal["none", "session"],
    request_metadata: RequestMetadata | None,
    reason: str,
    status_code: int,
) -> None:
    await record_audit(
        db_manager,
        action="agent.invoke.denied",
        target_type="agent",
        target_id=agent_name,
        outcome="denied",
        api_principal=principal,
        request_metadata=request_metadata,
        metadata={
            "memory_mode": memory_mode,
            "reason": reason,
            "status_code": status_code,
        },
    )

