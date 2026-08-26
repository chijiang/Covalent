"""Tests for token-level streaming aggregation and the durable run manager."""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

from covalent.model.base import ProviderConfig
from covalent.model.openai_compatible import OpenAICompatibleProvider
from covalent.runtime.run_manager import RunManager


class _FakeChunk:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._data


class _FakeCompletions:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    async def create(self, **_: Any) -> Any:
        outer = self

        class _Stream:
            def __aiter__(self):
                outer._iter = iter(outer._chunks)
                return self

            async def __anext__(self):
                try:
                    return _FakeChunk(next(outer._iter))
                except StopIteration:
                    raise StopAsyncIteration from None

        return _Stream()


class _FakeClient:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(chunks)


def _provider(chunks: list[dict[str, Any]]) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        ProviderConfig(provider="fake", model="fake-model", api_key="k", base_url="http://fake")
    )
    provider._client = _FakeClient(chunks)  # noqa: SLF001 — test seam
    return provider


class StreamGenerationAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_deltas_and_aggregated_response(self) -> None:
        provider = _provider(
            [
                {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo "}}]},
                {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
                {"choices": [{}], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
            ]
        )
        deltas: list[str] = []
        response = None
        async for tag, value in provider.stream_generation(_request([])):
            if tag == "delta":
                deltas.append(value)
            else:
                response = value
        self.assertEqual(deltas, ["Hel", "lo ", "world"])
        assert response is not None
        self.assertEqual(response.output_text, "Hello world")
        self.assertEqual(response.tool_calls, [])
        assert response.usage is not None
        self.assertEqual(response.usage.total_tokens, 8)
        self.assertEqual(response.assistant_message.role, "assistant")

    async def test_tool_call_fragments_concatenate(self) -> None:
        provider = _provider(
            [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":'}},
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '"cats"}'}},
                ]}}]},
            ]
        )
        response = None
        async for tag, value in provider.stream_generation(_request([])):
            if tag == "response":
                response = value
        assert response is not None
        self.assertEqual(len(response.tool_calls), 1)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.name, "search")
        self.assertEqual(call.arguments, {"q": "cats"})

    async def test_no_choices_raises_provider_error(self) -> None:
        from covalent.model.base import ModelProviderError

        provider = _provider([{"choices": []}])
        with self.assertRaises(ModelProviderError):
            async for _ in provider.stream_generation(_request([])):
                pass


def _request(messages: list[dict[str, Any]]) -> Any:
    from covalent.core.types import GenerationRequest

    return GenerationRequest(model="fake-model", messages=messages)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class RunManagerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import asyncio

        from sqlalchemy import pool, text
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from covalent.infra.migrations import run_database_migrations

        cls.database_url = os.environ["TEST_DATABASE_URL"]
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        cls.engine = create_async_engine(cls.database_url, poolclass=pool.NullPool)
        cls.session_factory = async_sessionmaker(cls.engine, expire_on_commit=False, class_=AsyncSession)

    async def asyncSetUp(self) -> None:
        from sqlalchemy import text

        async with self.session_factory() as session:
            await session.execute(text("TRUNCATE chat_run_events, chat_runs, chat_sessions RESTART IDENTITY CASCADE"))
            await session.commit()
        self.manager = RunManager(self.session_factory)
        self.session_counter = 0

    async def _create_session(self) -> str:
        from datetime import UTC, datetime

        from covalent.infra.db import ChatSessionRow

        self.session_counter += 1
        session_id = f"sess-{self.session_counter}"
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    ChatSessionRow(
                        id=session_id,
                        title="t",
                        agent_name="a",
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
        return session_id

    async def _create_run(self, session_id: str) -> str:
        run_id = f"run-{self.session_counter}"
        await self.manager.create_run(
            run_id=run_id,
            session_id=session_id,
            agent_name="a",
            owner_user_id=None,
            workspace_id=None,
            input_json={},
        )
        return run_id

    async def test_delta_batching_and_replay(self) -> None:
        session_id = await self._create_session()
        run_id = await self._create_run(session_id)
        for fragment in ["a", "b", "c"]:
            await self.manager.append_event(run_id, "assistant_delta", {"text": fragment})
        await self.manager.append_event(run_id, "final", {"output_text": "abc"})
        await self.manager.finish_run(run_id, "completed")

        events = [item async for item in self.manager.stream_events(run_id, 0)]
        # Three sub-threshold fragments coalesce into one batched delta row.
        names = [event for _, event, _ in events]
        self.assertEqual(names, ["assistant_delta", "final"])
        self.assertEqual(events[0][2], {"text": "abc"})
        run = await self.manager.get_run(run_id)
        assert run is not None
        self.assertEqual(run.status, "completed")

    async def test_replay_after_position_skips_earlier_events(self) -> None:
        session_id = await self._create_session()
        run_id = await self._create_run(session_id)
        await self.manager.append_event(run_id, "iteration", {"iteration": 1})
        await self.manager.append_event(run_id, "final", {"output_text": "done"})
        await self.manager.finish_run(run_id, "completed")
        events = [item async for item in self.manager.stream_events(run_id, 0)]
        final_position = events[-1][0]
        replayed = [item async for item in self.manager.stream_events(run_id, final_position)]
        self.assertEqual(replayed, [])

    async def test_cancel_running_run(self) -> None:
        session_id = await self._create_session()
        run_id = await self._create_run(session_id)
        started = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await self.manager.append_event(run_id, "cancelled", {"detail": "x"})
                raise

        self.manager.start_run(run_id, worker)
        await started.wait()
        result = await self.manager.cancel_run(run_id)
        self.assertEqual(result, "cancelling")
        for _ in range(40):
            run = await self.manager.get_run(run_id)
            if run is not None and run.status == "cancelled":
                break
            await asyncio.sleep(0.05)
        run = await self.manager.get_run(run_id)
        assert run is not None
        self.assertEqual(run.status, "cancelled")
        events = [item async for item in self.manager.stream_events(run_id, 0)]
        self.assertIn("cancelled", [event for _, event, _ in events])
        # Idempotent on a terminal run.
        self.assertEqual(await self.manager.cancel_run(run_id), "noop")
        self.assertEqual(await self.manager.cancel_run("missing-run"), "unknown")

    async def test_open_run_guard_and_sweep(self) -> None:
        session_id = await self._create_session()
        run_id = await self._create_run(session_id)
        open_run = await self.manager.open_run_for_session(session_id)
        assert open_run is not None
        self.assertEqual(open_run.id, run_id)
        await self.manager.finish_run(run_id, "completed")
        self.assertIsNone(await self.manager.open_run_for_session(session_id))

        # A stale 'running' row (server restart) is swept to failed with a
        # terminal error event.
        stale_session = await self._create_session()
        stale_run = await self._create_run(stale_session)
        swept = await self.manager.sweep_orphans()
        self.assertEqual(swept, 1)
        run = await self.manager.get_run(stale_run)
        assert run is not None
        self.assertEqual(run.status, "failed")
        events = [item async for item in self.manager.stream_events(stale_run, 0)]
        self.assertIn("error", [event for _, event, _ in events])
