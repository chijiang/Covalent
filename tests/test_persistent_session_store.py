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
