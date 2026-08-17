"""Lifecycle tests for the public streaming SSE generator.

Regression guard: a client that disconnects right after the first event
(run.created) must still release the per-token limiter slot and run
finalization — the try/finally spans the generator's whole lifetime,
starting at the acquire.
"""

from __future__ import annotations

import unittest
from typing import Any

from covalent.application.services.invoke_service import public_invoke_stream
from covalent.model.base import ModelProviderError


class _RecordingLimiter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, token_id: str) -> None:
        self.calls.append("acquired")

    async def release(self, token_id: str) -> None:
        self.calls.append("released")


async def _events(*items: Any):
    for item in items:
        if isinstance(item, Exception):
            raise item
        yield item


def _stream(events, limiter, **overrides):
    kwargs: dict[str, Any] = {
        "events": events,
        "run_id": "run-1",
        "agent_name": "test-agent",
        "memory_mode": "none",
        "session_id": None,
        "created_at_iso": "2026-08-17T00:00:00+00:00",
        "trace_level": "none",
        "limiter": limiter,
        "token_id": "tok-1",
        "record_run": None,
    }
    kwargs.update(overrides)
    return public_invoke_stream(**kwargs)


class PublicStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_after_first_event_releases_slot(self) -> None:
        """Consume run.created, then aclose() — exactly what a client
        disconnect does. The limiter slot must be released AND the run must
        not be recorded as a success (no final answer was produced)."""
        limiter = _RecordingLimiter()
        summaries: list[dict[str, Any]] = []

        async def record(summary: dict[str, Any]) -> None:
            summaries.append(summary)

        stream = _stream(
            _events({"event": "final", "payload": {"output_text": "done"}}),
            limiter,
            record_run=record,
        )
        first = await stream.__anext__()
        self.assertIn("run.created", first)

        await stream.aclose()

        self.assertEqual(limiter.calls, ["acquired", "released"])
        self.assertEqual(len(summaries), 1)
        # A client disconnect is not a successful run: it produced no final
        # answer, so it must not count toward success-rate/audit as completed.
        self.assertEqual(summaries[0]["status"], "aborted")
        self.assertIsNone(summaries[0]["final_payload"])

    async def test_disconnect_after_final_answer_stays_completed(self) -> None:
        """A disconnect AFTER the final answer was generated is a genuine
        success: the run produced its answer, the connection just dropped."""
        limiter = _RecordingLimiter()
        summaries: list[dict[str, Any]] = []

        async def record(summary: dict[str, Any]) -> None:
            summaries.append(summary)

        stream = _stream(
            _events({"event": "final", "payload": {"output_text": "done"}}),
            limiter,
            record_run=record,
        )
        # Consume created + the mapped final event, then disconnect before
        # run.completed is reached.
        await stream.__anext__()
        second = await stream.__anext__()
        self.assertTrue(any(key in second for key in ("final", "output_text")))

        await stream.aclose()

        self.assertEqual(summaries[0]["status"], "completed")
        self.assertEqual(summaries[0]["final_payload"]["output_text"], "done")

    async def test_full_stream_yields_created_events_completed(self) -> None:
        limiter = _RecordingLimiter()
        summaries: list[dict[str, Any]] = []

        async def record(summary: dict[str, Any]) -> None:
            summaries.append(summary)

        stream = _stream(
            _events(
                {"event": "assistant", "payload": {"text": "hi"}},
                {"event": "final", "payload": {"output_text": "done"}},
            ),
            limiter,
            record_run=record,
        )
        chunks = [chunk async for chunk in stream]

        self.assertTrue(any("run.created" in chunk for chunk in chunks))
        self.assertTrue(any("run.completed" in chunk for chunk in chunks))
        self.assertEqual(limiter.calls, ["acquired", "released"])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "completed")
        self.assertIsNotNone(summaries[0]["final_payload"])

    async def test_model_error_yields_failed_and_releases(self) -> None:
        limiter = _RecordingLimiter()
        summaries: list[dict[str, Any]] = []

        async def record(summary: dict[str, Any]) -> None:
            summaries.append(summary)

        stream = _stream(
            _events(ModelProviderError("test-provider", "provider exploded", status_code=429)),
            limiter,
            record_run=record,
        )
        chunks = [chunk async for chunk in stream]

        self.assertTrue(any("run.failed" in chunk for chunk in chunks))
        self.assertEqual(limiter.calls, ["acquired", "released"])
        self.assertEqual(summaries[0]["status"], "failed")

    async def test_recording_failure_still_releases_slot(self) -> None:
        limiter = _RecordingLimiter()

        async def failing_record(summary: dict[str, Any]) -> None:
            raise RuntimeError("db down")

        stream = _stream(
            _events({"event": "final", "payload": {"output_text": "done"}}),
            limiter,
            record_run=failing_record,
        )
        with self.assertRaises(RuntimeError):
            async for _ in stream:
                pass

        self.assertEqual(limiter.calls, ["acquired", "released"])


if __name__ == "__main__":
    unittest.main()
