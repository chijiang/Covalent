"""PG-gated migration test for delegate_runs / delegate_messages.

Mirrors the setup pattern of test_repositories.py: runs against a real
PostgreSQL instance pointed at by ``TEST_DATABASE_URL`` and auto-skips when
it is unset. Proves revision ``20260818_000027`` creates both tables and
that downgrade one step, then re-upgrade, is clean.
"""

from __future__ import annotations

import asyncio
import os
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.infra.migrations import run_database_migrations


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class DelegateMigrationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_upgrade_downgrade_upgrade_is_idempotent(self) -> None:
        # tables exist at head
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1 FROM delegate_runs LIMIT 1"))
            await session.execute(text("SELECT 1 FROM delegate_messages LIMIT 1"))
        # downgrade one step and back. alembic's env.py calls asyncio.run(),
        # which is illegal inside this test's running loop — run the commands
        # on a worker thread (no ambient loop there).
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", self.database_url.replace("+asyncpg", ""))
        await asyncio.to_thread(command.downgrade, cfg, "20260817_000026")
        await asyncio.to_thread(command.upgrade, cfg, "head")
        # tables exist again after the round-trip
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1 FROM delegate_runs LIMIT 1"))
            await session.execute(text("SELECT 1 FROM delegate_messages LIMIT 1"))
