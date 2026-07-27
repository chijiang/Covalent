"""Integration tests for the persistent session store.

These tests run against a real PostgreSQL instance pointed at by
``TEST_DATABASE_URL``. The whole case is auto-skipped when that env var is
unset, so it is safe to run as part of the normal unit-test suite without a DB.

The harness applies Alembic migrations to the test database and truncates the
``chat_messages``/``chat_sessions`` tables before and after each test.
"""

from __future__ import annotations

import asyncio
import os
import unittest

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_framework.infra.migrations import run_database_migrations


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class PersistentSessionStoreTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        # alembic/env.py ignores the URL argument and instead reads
        # AppSettings().database_url, which loads AGENT_FRAMEWORK_DATABASE_URL
        # (from .env -> the dev DB). Set the env var here so the migration
        # targets the test DB rather than the dev DB.
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        # NullPool: IsolatedAsyncioTestCase uses a fresh event loop per test
        # method, so connections must not outlive a single test (a pooled
        # connection would be bound to a closed loop and break the next test).
        cls.engine = create_async_engine(cls.database_url, poolclass=pool.NullPool)
        cls.session_factory = async_sessionmaker(
            cls.engine, expire_on_commit=False, class_=AsyncSession
        )

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.engine.dispose())

    async def asyncSetUp(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text("TRUNCATE chat_messages, chat_sessions RESTART IDENTITY CASCADE")
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text("TRUNCATE chat_messages, chat_sessions RESTART IDENTITY CASCADE")
            )
            await session.commit()

    def _store(self):
        from agent_framework.infra.memory import PersistentSessionStore

        return PersistentSessionStore(self.session_factory)

    async def test_chat_messages_table_exists(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                text("select count(*) from pg_tables where tablename = 'chat_messages'")
            )
        self.assertEqual(result.scalar(), 1)

    async def test_save_then_get_round_trips_messages_in_order(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        record = ChatSessionRecord(
            id="sess-1",
            title="t",
            created_at=now,
            updated_at=now,
            messages=[
                ChatTranscriptMessage(id="m1", role="user", content="hi", attachments=[]),
                ChatTranscriptMessage(
                    id="m2", role="assistant", content="hello", attachments=[{"k": "v"}]
                ),
            ],
        )

        saved = await store.save_session(record)
        self.assertEqual(saved.message_count, 2)

        loaded = await store.get_session("sess-1")
        self.assertIsNotNone(loaded)
        self.assertEqual([m.id for m in loaded.messages], ["m1", "m2"])
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertEqual(loaded.messages[0].content, "hi")
        self.assertEqual(loaded.messages[1].attachments, [{"k": "v"}])
        self.assertEqual(loaded.message_count, 2)

    async def test_resave_replaces_messages_without_duplicates(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[ChatTranscriptMessage(id="m3", role="user", content="again")],
            )
        )

        loaded = await store.get_session("sess-1")
        self.assertEqual([m.id for m in loaded.messages], ["m3"])
        self.assertEqual(loaded.message_count, 1)

        async with self.session_factory() as session:
            result = await session.execute(
                text("select count(*) from chat_messages where session_id = 'sess-1'")
            )
        self.assertEqual(result.scalar(), 1)

    async def test_delete_session_removes_messages_via_cascade(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )

        deleted = await store.delete_session("sess-1")
        self.assertTrue(deleted)

        async with self.session_factory() as session:
            result = await session.execute(
                text("select count(*) from chat_messages where session_id = 'sess-1'")
            )
        self.assertEqual(result.scalar(), 0)

    async def test_list_sessions_reports_message_count(self) -> None:
        from datetime import UTC, datetime

        from agent_framework.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="one",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )
        await store.save_session(
            ChatSessionRecord(
                id="sess-2",
                title="two",
                created_at=now,
                updated_at=now,
                messages=[],
            )
        )

        sessions = await store.list_sessions()
        by_id = {s.id: s.message_count for s in sessions}
        self.assertEqual(by_id.get("sess-1"), 2)
        self.assertEqual(by_id.get("sess-2"), 0)
