"""Durable chat run lifecycle: background execution, event log, replay, cancel.

A run's execution is decoupled from any SSE connection: the worker coroutine
persists every event to ``chat_run_events`` (delta-batched) and publishes it to
in-process subscriber queues. SSE views replay persisted events after a
``Last-Event-ID``/``after`` position and then tail the bus; disconnecting a
view never touches execution. Cancellation flips the run row to ``cancelling``
and cancels the worker task, which persists a ``cancelled`` terminal event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from covalent.infra.db import ChatRunEventRow, ChatRunRow

logger = logging.getLogger(__name__)

RUN_STATUS_RUNNING = "running"
RUN_STATUS_CANCELLING = "cancelling"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_FAILED = "failed"

#: Events that end an SSE view once sent. ``input_required`` is deliberately
#: absent: the worker still emits the trailing ``session`` snapshot event, which
#: is what actually closes the view.
RUN_TERMINAL_EVENTS = {"final", "error", "cancelled", "session", "parent_input_required"}

#: Delta batching bounds: consecutive assistant_delta fragments are merged into
#: one persisted row until either threshold trips, so token streaming does not
#: become one INSERT per token.
_DELTA_MAX_FRAGMENTS = 32
_DELTA_MAX_CHARS = 512


class RunManagerError(RuntimeError):
    pass


class RunManager:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._next_position: dict[str, int] = {}
        self._delta_buffers: dict[str, list[str]] = {}

    # -- run lifecycle ---------------------------------------------------

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_name: str,
        owner_user_id: str | None,
        workspace_id: str | None,
        input_json: dict[str, Any],
    ) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ChatRunRow(
                        id=run_id,
                        session_id=session_id,
                        agent_name=agent_name,
                        owner_user_id=owner_user_id,
                        workspace_id=workspace_id,
                        status=RUN_STATUS_RUNNING,
                        input_json=dict(input_json),
                    )
                )
                last = await session.execute(
                    select(ChatRunEventRow.position)
                    .where(ChatRunEventRow.run_id == run_id)
                    .order_by(ChatRunEventRow.position.desc())
                    .limit(1)
                )
                row = last.first()
                self._next_position[run_id] = (row[0] + 1) if row is not None else 1
        self._delta_buffers[run_id] = []

    def start_run(self, run_id: str, worker: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.create_task(self._run_worker(run_id, worker))
        self._tasks[run_id] = task

    async def _run_worker(self, run_id: str, worker: Callable[[], Awaitable[None]]) -> None:
        try:
            await worker()
        except asyncio.CancelledError:
            await self._append_event(run_id, "cancelled", {"detail": "Cancelled by user."})
            await self._terminate(run_id, RUN_STATUS_CANCELLED)
            raise
        except Exception:
            logger.exception("Chat run %s failed", run_id)
            await self._append_event(run_id, "error", {"status_code": 500, "detail": "Agent run failed unexpectedly."})
            await self._terminate(run_id, RUN_STATUS_FAILED)
        finally:
            self._tasks.pop(run_id, None)

    async def finish_run(self, run_id: str, status: str = RUN_STATUS_COMPLETED, error: dict[str, Any] | None = None) -> None:
        """Worker-declared termination (final / input_required / cancelled)."""
        await self._terminate(run_id, status, error=error)

    async def _terminate(self, run_id: str, status: str, error: dict[str, Any] | None = None) -> None:
        buffer = self._delta_buffers.pop(run_id, None)
        if buffer:
            await self._persist_event(run_id, "assistant_delta", {"text": "".join(buffer)})
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatRunRow)
                    .where(ChatRunRow.id == run_id)
                    .values(
                        status=status,
                        error_json=dict(error or {}),
                        finished_at=now,
                        updated_at=now,
                    )
                )
        self._next_position.pop(run_id, None)
        for queue in self._subscribers.pop(run_id, set()):
            await queue.put(None)

    # -- events ----------------------------------------------------------

    async def append_event(self, run_id: str, event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "assistant_delta":
            buffer = self._delta_buffers.setdefault(run_id, [])
            text = str(payload.get("text") or "")
            if text:
                buffer.append(text)
            if len(buffer) >= _DELTA_MAX_FRAGMENTS or sum(map(len, buffer)) >= _DELTA_MAX_CHARS:
                await self._flush_delta_buffer(run_id)
            return
        await self._flush_delta_buffer(run_id)
        await self._append_event(run_id, event_name, payload)

    async def _flush_delta_buffer(self, run_id: str) -> None:
        buffer = self._delta_buffers.get(run_id)
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if text:
            await self._append_event(run_id, "assistant_delta", {"text": text})

    async def _append_event(self, run_id: str, event_name: str, payload: dict[str, Any]) -> None:
        position = self._next_position.get(run_id, 1)
        self._next_position[run_id] = position + 1
        await self._persist_event(run_id, event_name, payload, position=position)
        for queue in list(self._subscribers.get(run_id, set())):
            await queue.put((position, event_name, payload))

    async def _persist_event(
        self, run_id: str, event_name: str, payload: dict[str, Any], position: int | None = None
    ) -> None:
        if position is None:
            position = self._next_position.get(run_id, 1)
            self._next_position[run_id] = position + 1
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ChatRunEventRow(
                        run_id=run_id,
                        position=position,
                        event=event_name,
                        payload=payload,
                    )
                )

    # -- queries ----------------------------------------------------------

    async def get_run(self, run_id: str) -> ChatRunRow | None:
        async with self._session_factory() as session:
            result = await session.execute(select(ChatRunRow).where(ChatRunRow.id == run_id))
            return result.scalar_one_or_none()

    async def open_run_for_session(self, session_id: str) -> ChatRunRow | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow)
                .where(
                    ChatRunRow.session_id == session_id,
                    ChatRunRow.status.in_([RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING]),
                )
                .order_by(ChatRunRow.created_at.desc())
            )
            return result.scalars().first()

    async def list_runs_for_session(self, session_id: str) -> list[ChatRunRow]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow)
                .where(ChatRunRow.session_id == session_id)
                .order_by(ChatRunRow.created_at.asc())
            )
            return list(result.scalars().all())

    # -- cancellation -------------------------------------------------------

    async def cancel_run(self, run_id: str) -> str:
        """Returns 'cancelling' | 'noop' (already terminal) | 'unknown'."""
        run = await self.get_run(run_id)
        if run is None:
            return "unknown"
        if run.status != RUN_STATUS_RUNNING:
            return "noop"
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatRunRow)
                    .where(ChatRunRow.id == run_id, ChatRunRow.status == RUN_STATUS_RUNNING)
                    .values(status=RUN_STATUS_CANCELLING)
                )
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            # No live task (e.g. process restarted with a stale 'running' row):
            # terminate through the normal path so subscribers and the log end cleanly.
            await self._append_event(run_id, "cancelled", {"detail": "Run was not executing."})
            await self._terminate(run_id, RUN_STATUS_CANCELLED)
        return "cancelling"

    # -- SSE view -----------------------------------------------------------

    async def stream_events(self, run_id: str, after_position: int = 0) -> AsyncIterator[tuple[int, str, dict[str, Any]]]:
        """Replay persisted events with position > after_position, then tail the
        live bus until a terminal event (or run completion sentinel)."""
        queue: asyncio.Queue = asyncio.Queue()
        subscribers = self._subscribers.setdefault(run_id, set())
        subscribers.add(queue)
        try:
            replayed_position = after_position
            saw_terminal = False
            async with self._session_factory() as session:
                result = await session.execute(
                    select(ChatRunEventRow)
                    .where(ChatRunEventRow.run_id == run_id, ChatRunEventRow.position > after_position)
                    .order_by(ChatRunEventRow.position.asc())
                )
                for row in result.scalars():
                    replayed_position = row.position
                    yield row.position, row.event, row.payload
                    if row.event in RUN_TERMINAL_EVENTS:
                        saw_terminal = True
            if saw_terminal:
                return
            run = await self.get_run(run_id)
            if run is None or run.status not in (RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING):
                # The run finished before this view subscribed and the replay
                # window ended without a terminal event (e.g. the client's
                # after-position already covered the whole log) — nothing will
                # ever arrive on the bus.
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                position, event_name, payload = item
                if position <= replayed_position:
                    continue
                yield position, event_name, payload
                if event_name in RUN_TERMINAL_EVENTS:
                    return
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    # -- startup -------------------------------------------------------------

    async def sweep_orphans(self) -> int:
        """Mark pre-boot 'running'/'cancelling' rows failed and append a terminal
        error event so reconnecting clients see the failure."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow.id).where(
                    ChatRunRow.status.in_([RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING])
                )
            )
            stale_ids = [row[0] for row in result.all()]
        for run_id in stale_ids:
            self._next_position.pop(run_id, None)
            await self._append_event(
                run_id,
                "error",
                {"status_code": 503, "detail": "Run was interrupted by a server restart."},
            )
            await self._terminate(run_id, RUN_STATUS_FAILED, error={"code": "server_restarted"})
        if stale_ids:
            logger.warning("Swept %d orphaned chat run(s) as failed", len(stale_ids))
        return len(stale_ids)
