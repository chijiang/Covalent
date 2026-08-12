"""Integration tests for the agent/mcp repositories.

These run against a real PostgreSQL instance pointed at by ``TEST_DATABASE_URL``
and auto-skip when it is unset. They exercise the real SQLAlchemy repository
paths that the API-layer tests bypass via ``_FakeConfigStore`` (see
test_agent_crud_api.py) — a regression guard for the ConfigStore extraction:
the ``_get_agents``/``_save_agents`` extraction shipped with both methods at
module level (never bound to ``AgentRepository``), which the fake-backed tests
could not catch.
"""

from __future__ import annotations

import asyncio
import os
import unittest

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.infra.agent_repository import AgentRepository
from covalent.infra.config_store import PersistedAgentConfig
from covalent.infra.mcp_repository import McpRepository
from covalent.infra.migrations import run_database_migrations
from covalent.model.base import ProviderConfig

_REPO_TABLES = (
    "agents, agent_capabilities, agent_skills, agent_delegates, "
    "agent_mcp_servers, agent_mcp_tools, mcp_servers, mcp_server_env_vars"
)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class RepositoryTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def asyncSetUp(self) -> None:
        async with self.session_factory() as session:
            await session.execute(text(f"TRUNCATE {_REPO_TABLES} RESTART IDENTITY CASCADE"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        async with self.session_factory() as session:
            await session.execute(text(f"TRUNCATE {_REPO_TABLES} RESTART IDENTITY CASCADE"))
            await session.commit()

    def _agent_payload(self, name: str = "test-agent") -> dict[str, object]:
        return PersistedAgentConfig(
            name=name,
            description="regression test agent",
            system_prompt="You are a test agent.",
            provider=ProviderConfig(
                provider="openai_compatible",
                model="test-model",
                base_url="https://example.com",
            ),
        ).model_dump(mode="json")

    async def test_agent_repository_roundtrip(self) -> None:
        repo = AgentRepository(self.session_factory)
        assert await repo._get_agents(None) == []
        await repo._save_agents([self._agent_payload()], None)
        agents = await repo._get_agents(None)
        assert len(agents) == 1
        assert agents[0]["name"] == "test-agent"
        assert agents[0]["provider"]["model"] == "test-model"
        assert agents[0]["enabled"] is True

    async def test_mcp_repository_roundtrip(self) -> None:
        repo = McpRepository(self.session_factory)
        assert await repo.list_servers(None) == []
        await repo.save_servers(
            [
                {
                    "name": "docs",
                    "transport": "streamable_http",
                    "url": "https://example.com/mcp",
                    "env": {"API_KEY": "secret"},
                }
            ],
            None,
        )
        servers = await repo.list_servers(None)
        assert len(servers) == 1
        assert servers[0]["name"] == "docs"
        assert servers[0]["env"] == {"API_KEY": "secret"}
