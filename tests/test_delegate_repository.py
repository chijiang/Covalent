"""Delegate run store contract tests — in-memory always, PostgreSQL gated.

The behavioral contract (optimistic CAS transitions, private message fidelity,
direct-parent scoping, expiry/staleness filters, bulk scope release) is defined
once in ``_DelegateRunStoreContract`` and executed against both stores:
``InMemoryDelegateRunStore`` always, and ``PostgresDelegateRunStore`` against
real PostgreSQL when ``TEST_DATABASE_URL`` is set (same convention as
``test_repositories.py``).
"""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.core.types import DelegateRunStatus, Message, ParentInputRequest
from covalent.infra.db import ChatSessionRow
from covalent.infra.delegate_repository import (
    DelegateRunConflictError,
    DelegateRunMissingError,
    DelegateRunRecord,
    DelegateRunStore,
    InMemoryDelegateRunStore,
    PostgresDelegateRunStore,
)
from covalent.infra.migrations import run_database_migrations

_FIXED_NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


class _DelegateRunStoreContract:
    """Shared behavioral suite for every ``DelegateRunStore`` implementation."""

    def make_store(self) -> DelegateRunStore:
        raise NotImplementedError

    async def _seed_session(self, session_id: str) -> None:
        """Ensure a parent chat_sessions row exists (needed for the PG FK)."""

    async def asyncSetUp(self) -> None:
        self.store = self.make_store()

    def _record(self, **overrides: Any) -> DelegateRunRecord:
        values: dict[str, Any] = dict(
            id=f"run-{uuid.uuid4().hex[:12]}",
            session_id=None,
            execution_scope_id="scope-1",
            workspace_scope_id="ws-1",
            workspace_id=None,
            root_agent_name="root",
            parent_agent_name="root",
            parent_delegate_run_id=None,
            delegate_agent_name="researcher",
            origin_tool_call_id=None,
            status=DelegateRunStatus.CREATED,
            pending_request=None,
            latest_output="",
            summary="",
            error={},
            release_reason="",
            version=1,
            created_at=_FIXED_NOW,
            last_activity_at=_FIXED_NOW,
            released_at=None,
            expires_at=None,
        )
        values.update(overrides)
        return DelegateRunRecord(**values)

    # --- runs ---------------------------------------------------------------

    async def test_create_and_get_round_trip(self) -> None:
        request = ParentInputRequest(id="req-1", delegate_run_id="run-rt", title="Need input")
        record = self._record(
            id="run-rt",
            latest_output="partial",
            summary="doing things",
            error={"code": "boom"},
            pending_request=request,
            expires_at=_FIXED_NOW + timedelta(hours=1),
        )
        stored = await self.store.create_run(record)
        self.assertEqual(stored, record)
        loaded = await self.store.get_run("run-rt")
        self.assertEqual(loaded, record)
        self.assertIsNone(await self.store.get_run("missing-run"))

    async def test_delete_run_removes_messages(self) -> None:
        created = await self.store.create_run(self._record())
        await self.store.save_messages(created.id, [Message(role="user", content="hi")])
        await self.store.delete_run(created.id)
        self.assertIsNone(await self.store.get_run(created.id))
        self.assertEqual(await self.store.load_messages(created.id), [])

    async def test_transition_bumps_version_and_updates_fields(self) -> None:
        created = await self.store.create_run(self._record())
        updated = await self.store.transition_run(
            created.id,
            expected_version=created.version,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.RUNNING,
            latest_output="out",
            summary="sum",
        )
        self.assertEqual(updated.status, DelegateRunStatus.RUNNING)
        self.assertEqual(updated.version, created.version + 1)
        self.assertEqual(updated.latest_output, "out")
        self.assertEqual(updated.summary, "sum")

    async def test_transition_updates_payload_fields(self) -> None:
        created = await self.store.create_run(self._record())
        request = ParentInputRequest(
            id="req-2", delegate_run_id=created.id, tool_call_id="call-9", title="Need input"
        )
        waiting = await self.store.transition_run(
            created.id,
            expected_version=1,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.WAITING_PARENT,
            pending_request=request,
        )
        self.assertEqual(waiting.pending_request, request)
        idle = await self.store.transition_run(
            created.id,
            expected_version=waiting.version,
            from_statuses=(DelegateRunStatus.WAITING_PARENT,),
            status=DelegateRunStatus.IDLE,
            clear_pending=True,
            expires_at=_FIXED_NOW + timedelta(hours=2),
        )
        self.assertIsNone(idle.pending_request)
        self.assertEqual(idle.expires_at, _FIXED_NOW + timedelta(hours=2))
        failed = await self.store.transition_run(
            created.id,
            expected_version=idle.version,
            from_statuses=(DelegateRunStatus.IDLE,),
            status=DelegateRunStatus.FAILED,
            error={"code": "boom"},
            release_reason="agent error",
            released_at=_FIXED_NOW + timedelta(minutes=1),
        )
        self.assertEqual(failed.status, DelegateRunStatus.FAILED)
        self.assertEqual(failed.error, {"code": "boom"})
        self.assertEqual(failed.release_reason, "agent error")
        self.assertEqual(failed.released_at, _FIXED_NOW + timedelta(minutes=1))

    async def test_concurrent_transition_has_exactly_one_winner(self) -> None:
        created = await self.store.create_run(self._record())
        results = await asyncio.gather(
            self.store.transition_run(
                created.id,
                expected_version=1,
                from_statuses=(DelegateRunStatus.CREATED,),
                status=DelegateRunStatus.RUNNING,
            ),
            self.store.transition_run(
                created.id,
                expected_version=1,
                from_statuses=(DelegateRunStatus.CREATED,),
                status=DelegateRunStatus.CANCELLED,
            ),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, DelegateRunConflictError)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        final = await self.store.get_run(created.id)
        assert final is not None
        self.assertIn(final.status, (DelegateRunStatus.RUNNING, DelegateRunStatus.CANCELLED))

    async def test_stale_expected_version_raises_conflict(self) -> None:
        created = await self.store.create_run(self._record())
        await self.store.transition_run(
            created.id,
            expected_version=1,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.RUNNING,
        )
        with self.assertRaises(DelegateRunConflictError):
            await self.store.transition_run(
                created.id,
                expected_version=1,
                from_statuses=(DelegateRunStatus.CREATED, DelegateRunStatus.RUNNING),
                status=DelegateRunStatus.IDLE,
            )

    async def test_transition_rejects_wrong_source_status(self) -> None:
        created = await self.store.create_run(self._record())
        idle = await self.store.transition_run(
            created.id,
            expected_version=1,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.IDLE,
        )
        running = await self.store.transition_run(
            created.id,
            expected_version=idle.version,
            from_statuses=(DelegateRunStatus.IDLE,),
            status=DelegateRunStatus.RUNNING,
        )
        self.assertEqual(running.status, DelegateRunStatus.RUNNING)
        released = await self.store.transition_run(
            created.id,
            expected_version=running.version,
            from_statuses=(DelegateRunStatus.RUNNING,),
            status=DelegateRunStatus.RELEASED,
        )
        # released is terminal: an active-source transition must be rejected
        with self.assertRaises(DelegateRunConflictError):
            await self.store.transition_run(
                created.id,
                expected_version=released.version,
                from_statuses=(DelegateRunStatus.CREATED, DelegateRunStatus.RUNNING),
                status=DelegateRunStatus.RUNNING,
            )

    async def test_transition_unknown_run_raises_missing(self) -> None:
        with self.assertRaises(DelegateRunMissingError):
            await self.store.transition_run(
                "missing-run",
                expected_version=1,
                from_statuses=(DelegateRunStatus.CREATED,),
                status=DelegateRunStatus.RUNNING,
            )

    # --- messages -----------------------------------------------------------

    async def test_messages_round_trip_every_field(self) -> None:
        created = await self.store.create_run(self._record())
        messages = [
            Message(role="system", content="be brief"),
            Message(role="user", content="hello there"),
            Message(
                role="assistant",
                content="I will ask.",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "ask_parent", "arguments": '{"q": 1}'},
                    }
                ],
            ),
            Message(
                role="tool",
                content="parent said no",
                name="ask_parent",
                tool_call_id="call_1",
                reasoning_content="why not",
            ),
        ]
        await self.store.save_messages(created.id, messages)
        self.assertEqual(await self.store.load_messages(created.id), messages)
        self.assertEqual(await self.store.count_messages(created.id), 4)

    async def test_save_messages_replaces_atomically(self) -> None:
        created = await self.store.create_run(self._record())
        long = [Message(role="user", content=f"m{i}") for i in range(4)]
        await self.store.save_messages(created.id, long)
        self.assertEqual(await self.store.count_messages(created.id), 4)
        short = [Message(role="user", content="only"), Message(role="assistant", content="one")]
        await self.store.save_messages(created.id, short)
        self.assertEqual(await self.store.load_messages(created.id), short)
        self.assertEqual(await self.store.count_messages(created.id), 2)
        await self.store.delete_messages(created.id)
        self.assertEqual(await self.store.load_messages(created.id), [])
        self.assertEqual(await self.store.count_messages(created.id), 0)

    # --- scoping ------------------------------------------------------------

    async def test_list_children_scopes_to_direct_parent(self) -> None:
        await self.store.create_run(self._record(id="run-parent", parent_agent_name="root"))
        await self.store.create_run(
            self._record(
                id="run-other-parent", parent_agent_name="other", delegate_agent_name="planner"
            )
        )
        await self.store.create_run(
            self._record(id="run-child-a", parent_delegate_run_id="run-parent")
        )
        await self.store.create_run(
            self._record(
                id="run-child-b",
                parent_delegate_run_id="run-parent",
                created_at=_FIXED_NOW + timedelta(minutes=1),
            )
        )
        # sibling parent's child: same scope, different parent run/agent
        await self.store.create_run(
            self._record(
                id="run-child-c",
                parent_delegate_run_id="run-other-parent",
                parent_agent_name="other",
            )
        )
        # same parent run, different execution scope
        await self.store.create_run(
            self._record(
                id="run-child-d", execution_scope_id="scope-2", parent_delegate_run_id="run-parent"
            )
        )

        children = await self.store.list_children(
            execution_scope_id="scope-1",
            parent_agent_name="root",
            parent_delegate_run_id="run-parent",
        )
        self.assertEqual([child.id for child in children], ["run-child-a", "run-child-b"])

        roots = await self.store.list_children(
            execution_scope_id="scope-1",
            parent_agent_name="root",
            parent_delegate_run_id=None,
        )
        self.assertEqual([root.id for root in roots], ["run-parent"])

        self.assertEqual(
            await self.store.count_active_by_parent(
                execution_scope_id="scope-1",
                parent_agent_name="root",
                parent_delegate_run_id="run-parent",
            ),
            2,
        )
        await self.store.transition_run(
            "run-child-a",
            expected_version=1,
            from_statuses=(DelegateRunStatus.CREATED,),
            status=DelegateRunStatus.RELEASED,
        )
        self.assertEqual(
            await self.store.count_active_by_parent(
                execution_scope_id="scope-1",
                parent_agent_name="root",
                parent_delegate_run_id="run-parent",
            ),
            1,
        )
        self.assertEqual(await self.store.count_by_execution_scope("scope-1"), 5)
        self.assertEqual(await self.store.count_by_execution_scope("scope-2"), 1)

    async def test_release_scope_marks_active_released(self) -> None:
        await self.store.create_run(
            self._record(id="run-ra", execution_scope_id="scope-rel")
        )
        await self.store.create_run(
            self._record(
                id="run-rr",
                execution_scope_id="scope-rel",
                status=DelegateRunStatus.RUNNING,
            )
        )
        await self.store.create_run(
            self._record(
                id="run-rrl",
                execution_scope_id="scope-rel",
                status=DelegateRunStatus.RELEASED,
                release_reason="done",
                released_at=_FIXED_NOW,
            )
        )
        await self.store.create_run(self._record(id="run-rb", execution_scope_id="scope-other"))

        released_at = _FIXED_NOW + timedelta(minutes=5)
        count = await self.store.release_scope(
            "scope-rel", reason="session ended", released_at=released_at
        )
        self.assertEqual(count, 2)

        for run_id in ("run-ra", "run-rr"):
            row = await self.store.get_run(run_id)
            assert row is not None
            self.assertEqual(row.status, DelegateRunStatus.RELEASED)
            self.assertEqual(row.release_reason, "session ended")
            self.assertEqual(row.released_at, released_at)
            self.assertEqual(row.version, 1)  # bulk release, not a CAS bump

        untouched = await self.store.get_run("run-rb")
        assert untouched is not None
        self.assertEqual(untouched.status, DelegateRunStatus.CREATED)
        already = await self.store.get_run("run-rrl")
        assert already is not None
        self.assertEqual(already.status, DelegateRunStatus.RELEASED)
        self.assertEqual(already.release_reason, "done")
        self.assertEqual(already.released_at, _FIXED_NOW)

    async def test_list_expirable_and_stale_running_filters(self) -> None:
        stale_a = await self.store.create_run(
            self._record(
                status=DelegateRunStatus.RUNNING,
                created_at=_FIXED_NOW - timedelta(hours=2),
                last_activity_at=_FIXED_NOW - timedelta(hours=2),
            )
        )
        stale_b = await self.store.create_run(
            self._record(
                status=DelegateRunStatus.RUNNING,
                created_at=_FIXED_NOW - timedelta(hours=3),
                last_activity_at=_FIXED_NOW - timedelta(hours=3),
            )
        )
        await self.store.create_run(self._record(status=DelegateRunStatus.RUNNING))
        # old activity but not running -> not stale-running
        await self.store.create_run(
            self._record(
                status=DelegateRunStatus.IDLE,
                last_activity_at=_FIXED_NOW - timedelta(hours=2),
            )
        )

        stale = await self.store.list_stale_running(older_than=_FIXED_NOW)
        self.assertEqual({row.id for row in stale}, {stale_a.id, stale_b.id})
        limited = await self.store.list_stale_running(older_than=_FIXED_NOW, limit=1)
        self.assertEqual(len(limited), 1)
        self.assertIn(limited[0].id, {stale_a.id, stale_b.id})

        idle_due = await self.store.create_run(
            self._record(
                status=DelegateRunStatus.IDLE, expires_at=_FIXED_NOW - timedelta(minutes=1)
            )
        )
        # idle deadline after idle_before: excluded even though it precedes
        # waiting_before — thresholds match per status
        await self.store.create_run(
            self._record(
                status=DelegateRunStatus.IDLE, expires_at=_FIXED_NOW + timedelta(minutes=30)
            )
        )
        waiting_due = await self.store.create_run(
            self._record(
                status=DelegateRunStatus.WAITING_PARENT,
                expires_at=_FIXED_NOW - timedelta(minutes=1),
            )
        )
        created_due = await self.store.create_run(
            self._record(
                status=DelegateRunStatus.CREATED, expires_at=_FIXED_NOW - timedelta(minutes=1)
            )
        )
        # running rows never expire via this path, and no deadline -> never
        await self.store.create_run(
            self._record(
                status=DelegateRunStatus.RUNNING, expires_at=_FIXED_NOW - timedelta(minutes=1)
            )
        )
        await self.store.create_run(self._record(status=DelegateRunStatus.IDLE, expires_at=None))

        expirable = await self.store.list_expirable(
            idle_before=_FIXED_NOW, waiting_before=_FIXED_NOW + timedelta(hours=1)
        )
        self.assertEqual(
            {row.id for row in expirable},
            {idle_due.id, waiting_due.id, created_due.id},
        )

    async def test_active_session_scope_and_agent_filters(self) -> None:
        await self._seed_session("session-1")
        await self._seed_session("session-2")
        await self.store.create_run(
            self._record(
                id="run-s1-a",
                session_id="session-1",
                execution_scope_id="scope-x",
                status=DelegateRunStatus.RUNNING,
            )
        )
        await self.store.create_run(
            self._record(
                id="run-s1-b",
                session_id="session-1",
                execution_scope_id="scope-x",
                delegate_agent_name="writer",
                status=DelegateRunStatus.IDLE,
            )
        )
        await self.store.create_run(
            self._record(
                id="run-s1-c",
                session_id="session-1",
                execution_scope_id="scope-x",
                status=DelegateRunStatus.RELEASED,
            )
        )
        await self.store.create_run(
            self._record(
                id="run-s2-a",
                session_id="session-2",
                execution_scope_id="scope-y",
                status=DelegateRunStatus.RUNNING,
            )
        )
        await self.store.create_run(
            self._record(
                id="run-s1-d",
                session_id="session-1",
                execution_scope_id="scope-x",
                status=DelegateRunStatus.WAITING_PARENT,
            )
        )

        by_session = await self.store.list_active_by_session("session-1")
        self.assertEqual({row.id for row in by_session}, {"run-s1-a", "run-s1-b", "run-s1-d"})
        self.assertEqual(await self.store.list_active_by_session("session-none"), [])

        by_scope = await self.store.list_active_by_execution_scope("scope-x")
        self.assertEqual({row.id for row in by_scope}, {"run-s1-a", "run-s1-b", "run-s1-d"})

        by_agent = await self.store.list_active_for_agent("researcher")
        self.assertEqual({row.id for row in by_agent}, {"run-s1-a", "run-s2-a", "run-s1-d"})
        self.assertEqual(
            {row.id for row in await self.store.list_active_for_agent("writer")},
            {"run-s1-b"},
        )


class InMemoryDelegateRunStoreTests(_DelegateRunStoreContract, unittest.IsolatedAsyncioTestCase):
    def make_store(self) -> DelegateRunStore:
        return InMemoryDelegateRunStore()


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class PostgresDelegateRunStoreTests(_DelegateRunStoreContract, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        # alembic/env.py reads AppSettings().database_url instead of the URL
        # argument; point it at the test DB so migrations never touch the dev DB.
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        # NullPool: IsolatedAsyncioTestCase uses a fresh event loop per test
        # method, so connections must not outlive a single test.
        cls.engine = create_async_engine(cls.database_url, poolclass=pool.NullPool)
        cls.session_factory = async_sessionmaker(
            cls.engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.engine.dispose())

    def make_store(self) -> DelegateRunStore:
        return PostgresDelegateRunStore(self.session_factory)

    async def _seed_session(self, session_id: str) -> None:
        async with self.session_factory() as session:
            session.add(ChatSessionRow(id=session_id))
            await session.commit()

    async def asyncSetUp(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text("TRUNCATE chat_sessions, delegate_runs, delegate_messages RESTART IDENTITY CASCADE")
            )
            await session.commit()
        await super().asyncSetUp()
