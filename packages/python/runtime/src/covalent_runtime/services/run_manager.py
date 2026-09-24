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
from typing import Any


from covalent_runtime.ports.runs import RunRecord, RunStore

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

#: 思考片段粒度更细（常为单字符），用更大的阈值合并，避免逐 token 落库。
_REASONING_MAX_FRAGMENTS = 128
_REASONING_MAX_CHARS = 2048


def _group_reasoning_fragments(
    fragments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """合并相邻同名思考片段，保持事件到达顺序（reasoning_delta 与
    delegate_reasoning_delta 各保留自己的事件名）。"""
    grouped: list[tuple[str, list[str]]] = []
    for name, text in fragments:
        if grouped and grouped[-1][0] == name:
            grouped[-1][1].append(text)
        else:
            grouped.append((name, [text]))
    return [(name, merged) for name, texts in grouped if (merged := "".join(texts))]


class RunManagerError(RuntimeError):
    pass


class RunManager:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._next_position: dict[str, int] = {}
        self._delta_buffers: dict[str, list[str]] = {}
        self._reasoning_buffers: dict[str, list[tuple[str, str]]] = {}

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
        self._next_position[run_id] = await self.store.create_run(
            run_id=run_id, session_id=session_id, agent_name=agent_name,
            owner_user_id=owner_user_id, workspace_id=workspace_id, input_json=input_json,
        )
        self._delta_buffers[run_id] = []
        self._reasoning_buffers[run_id] = []

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
        reasoning_buffer = self._reasoning_buffers.pop(run_id, None)
        if reasoning_buffer:
            for name, merged in _group_reasoning_fragments(reasoning_buffer):
                await self._persist_event(run_id, name, {"text": merged})
        await self.store.finish_run(run_id, status, error)
        self._next_position.pop(run_id, None)
        for queue in self._subscribers.pop(run_id, set()):
            await queue.put(None)

    # -- events ----------------------------------------------------------

    async def append_event(self, run_id: str, event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "assistant_delta":
            # 冲刷思考缓冲在前，保证落库位置时序与事件到达顺序一致。
            await self._flush_reasoning_buffer(run_id)
            buffer = self._delta_buffers.setdefault(run_id, [])
            text = str(payload.get("text") or "")
            if text:
                buffer.append(text)
            if len(buffer) >= _DELTA_MAX_FRAGMENTS or sum(map(len, buffer)) >= _DELTA_MAX_CHARS:
                await self._flush_delta_buffer(run_id)
            return
        if event_name in ("reasoning_delta", "delegate_reasoning_delta"):
            # 两个名字共用一份缓冲（保留各自事件名，flush 时按到达顺序分组），
            # 否则带前缀的 child 思考会逐 token 落库并逐条推给前端。
            await self._flush_delta_buffer(run_id)
            buffer = self._reasoning_buffers.setdefault(run_id, [])
            text = str(payload.get("text") or "")
            if text:
                buffer.append((event_name, text))
            if len(buffer) >= _REASONING_MAX_FRAGMENTS or sum(len(t) for _, t in buffer) >= _REASONING_MAX_CHARS:
                await self._flush_reasoning_buffer(run_id)
            return
        await self._flush_delta_buffer(run_id)
        await self._flush_reasoning_buffer(run_id)
        await self._append_event(run_id, event_name, payload)

    async def _flush_delta_buffer(self, run_id: str) -> None:
        buffer = self._delta_buffers.get(run_id)
        if not buffer:
            return
        text = "".join(buffer)
        buffer.clear()
        if text:
            await self._append_event(run_id, "assistant_delta", {"text": text})

    async def _flush_reasoning_buffer(self, run_id: str) -> None:
        buffer = self._reasoning_buffers.get(run_id)
        if not buffer:
            return
        pending, buffer[:] = list(buffer), []
        for name, merged in _group_reasoning_fragments(pending):
            await self._append_event(run_id, name, {"text": merged})

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
        await self.store.append_event(run_id, event_name, payload, position)

    # -- queries ----------------------------------------------------------

    async def get_run(self, run_id: str) -> RunRecord | None:
        return await self.store.get_run(run_id)

    async def open_run_for_session(self, session_id: str) -> RunRecord | None:
        return await self.store.open_run_for_session(session_id)

    async def list_runs_for_session(self, session_id: str) -> list[RunRecord]:
        return await self.store.list_runs_for_session(session_id)

    # -- cancellation -------------------------------------------------------

    async def cancel_run(self, run_id: str) -> str:
        """Returns 'cancelling' | 'noop' (already terminal) | 'unknown'."""
        run = await self.get_run(run_id)
        if run is None:
            return "unknown"
        if run.status != RUN_STATUS_RUNNING:
            return "noop"
        await self.store.mark_cancelling(run_id)
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
            for position, event_name, payload in await self.store.events_after(run_id, after_position):
                replayed_position = position
                yield position, event_name, payload
                if event_name in RUN_TERMINAL_EVENTS:
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
        stale_ids = await self.store.stale_run_ids()
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
