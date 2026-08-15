"""Integration tests for the sandbox profile/binding repository.

These run against a real PostgreSQL instance pointed at by ``TEST_DATABASE_URL``
and auto-skip when it is unset (same convention as ``test_repositories.py``).
They cover the persistence slice of the per-agent sandbox profiles design:
profile round-trip, logical instance bindings for session and run scopes,
cascade/restrict behavior, and the agent ``sandbox_profile_id`` reference.
"""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from sqlalchemy import pool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.infra.agent_repository import AgentRepository
from covalent.infra.config_store import PersistedAgentConfig
from covalent.infra.db import ChatSessionRow
from covalent.infra.migrations import run_database_migrations
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.model.base import ProviderConfig

_SANDBOX_TABLES = (
    "sandbox_instances, sandbox_profiles, agents, agent_capabilities, "
    "agent_skills, agent_delegates, agent_mcp_servers, agent_mcp_tools, "
    "mcp_servers, mcp_server_env_vars, chat_sessions"
)


def _profile_payload(profile_id: str = "profile-python-312", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": profile_id,
        "name": "Python 3.12",
        "description": "Default Python sandbox",
        "workspace_id": None,
        "image": "covalent-sandbox:dev",
        "pull_policy": "if_not_present",
        "keepalive_command": ["tail", "-f", "/dev/null"],
        "runtime_capabilities": ["python", "shell"],
        "contract_version": 1,
        "memory_limit": "512m",
        "pids_limit": 256,
        "cpus": 1.0,
        "tmpfs_size": "128m",
        "enabled": True,
        "is_default": True,
        "revision": 1,
        "validation_status": "valid",
        "validated_image_id": None,
        "validated_image_digest": None,
        "validated_at": None,
        "validation_message": None,
    }
    payload.update(overrides)
    return payload


def _spec_snapshot(profile_id: str = "profile-python-312", revision: int = 1) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "profile_revision": revision,
        "image": "covalent-sandbox:dev",
        "image_reference": "covalent-sandbox:dev",
        "keepalive_command": ["tail", "-f", "/dev/null"],
        "runtime_capabilities": ["python", "shell"],
        "contract_version": 1,
        "resources": {
            "memory_limit": "512m",
            "pids_limit": 256,
            "cpus": 1.0,
            "tmpfs_size": "128m",
        },
    }


def _binding_payload(
    *,
    execution_scope_id: str = "session-1",
    session_id: str | None = "session-1",
    scope_kind: str = "session",
    agent_name: str = "master",
    profile_id: str = "profile-python-312",
) -> dict[str, object]:
    return {
        "id": f"sbx-{uuid.uuid4().hex[:20]}",
        "execution_scope_id": execution_scope_id,
        "session_id": session_id,
        "scope_kind": scope_kind,
        "agent_name": agent_name,
        "profile_id": profile_id,
        "profile_name_snapshot": "Python 3.12",
        "profile_revision": 1,
        "spec_snapshot": _spec_snapshot(profile_id),
        "allowed_outbound_snapshot": [],
    }


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class SandboxRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        cls.engine = create_async_engine(cls.database_url, poolclass=pool.NullPool)
        cls.session_factory = async_sessionmaker(cls.engine, expire_on_commit=False, class_=AsyncSession)

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls.engine.dispose())

    async def asyncSetUp(self) -> None:
        async with self.session_factory() as session:
            await session.execute(text(f"TRUNCATE {_SANDBOX_TABLES} RESTART IDENTITY CASCADE"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        async with self.session_factory() as session:
            await session.execute(text(f"TRUNCATE {_SANDBOX_TABLES} RESTART IDENTITY CASCADE"))
            await session.commit()

    async def _seed_profile(self, repo: SandboxRepository, **overrides: object) -> dict[str, object]:
        return await repo.create_profile(_profile_payload(**overrides))

    async def _seed_session(self, session_id: str) -> None:
        async with self.session_factory() as session:
            session.add(ChatSessionRow(id=session_id, agent_name="master"))
            await session.commit()

    # --- profiles -------------------------------------------------------

    async def test_profile_roundtrip_and_revision_fields(self) -> None:
        repo = SandboxRepository(self.session_factory)
        created = await self._seed_profile(repo)
        assert created["id"] == "profile-python-312"
        assert created["runtime_capabilities"] == ["python", "shell"]
        assert created["keepalive_command"] == ["tail", "-f", "/dev/null"]
        assert created["revision"] == 1
        assert created["is_default"] is True

        fetched = await repo.get_profile("profile-python-312")
        assert fetched is not None
        assert fetched["image"] == "covalent-sandbox:dev"
        assert fetched["validation_status"] == "valid"

        listed = await repo.list_profiles()
        assert [p["id"] for p in listed] == ["profile-python-312"]

        updated = await repo.update_profile(
            "profile-python-312",
            {"revision": 2, "image": "covalent-sandbox:py312", "memory_limit": "1g"},
        )
        assert updated is not None
        assert updated["revision"] == 2
        assert updated["image"] == "covalent-sandbox:py312"
        assert updated["memory_limit"] == "1g"
        # Untouched fields keep their values.
        assert updated["pids_limit"] == 256

        assert await repo.get_profile("missing") is None

    async def test_profile_delete_restricted_while_referenced(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_session("session-1")
        await repo.create_binding(_binding_payload())

        with self.assertRaises(IntegrityError):
            await repo.delete_profile("profile-python-312")

        references = await repo.count_profile_references("profile-python-312")
        assert references == {"agents": 0, "instances": 1}

    async def test_unreferenced_profile_deletes(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        assert await repo.delete_profile("profile-python-312") is True
        assert await repo.get_profile("profile-python-312") is None

    # --- bindings -------------------------------------------------------

    async def test_session_scope_binding_roundtrip(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_session("session-1")

        assert await repo.get_binding("session-1", "master") is None
        created = await repo.create_binding(_binding_payload())
        assert created["scope_kind"] == "session"
        assert created["session_id"] == "session-1"

        fetched = await repo.get_binding("session-1", "master")
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["spec_snapshot"]["profile_id"] == "profile-python-312"

        sibling = await repo.create_binding(
            _binding_payload(agent_name="delegate", execution_scope_id="session-1", session_id="session-1")
        )
        assert sibling["agent_name"] == "delegate"
        by_scope = await repo.list_bindings_by_scope("session-1")
        assert {b["agent_name"] for b in by_scope} == {"master", "delegate"}
        by_session = await repo.list_bindings_by_session("session-1")
        assert {b["agent_name"] for b in by_session} == {"master", "delegate"}

    async def test_binding_unique_per_scope_and_agent(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_session("session-1")
        await repo.create_binding(_binding_payload())

        with self.assertRaises(IntegrityError):
            await repo.create_binding(_binding_payload())

    async def test_run_scope_binding_needs_no_chat_session(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo, is_default=False)
        # No chat_sessions row exists for run-42.
        created = await repo.create_binding(
            _binding_payload(
                execution_scope_id="run-42",
                session_id=None,
                scope_kind="run",
                agent_name="master",
            )
        )
        assert created["scope_kind"] == "run"
        assert created["session_id"] is None

        fetched = await repo.get_binding("run-42", "master")
        assert fetched is not None
        assert fetched["execution_scope_id"] == "run-42"

    async def test_run_scope_binding_rejects_session_id(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo, is_default=False)
        with self.assertRaises(IntegrityError):
            await repo.create_binding(
                _binding_payload(
                    execution_scope_id="run-43",
                    session_id="run-43",
                    scope_kind="run",
                )
            )

    async def test_session_delete_cascades_bindings(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_session("session-1")
        await repo.create_binding(_binding_payload())

        async with self.session_factory() as session:
            row = await session.get(ChatSessionRow, "session-1")
            assert row is not None
            await session.delete(row)
            await session.commit()

        assert await repo.get_binding("session-1", "master") is None

    async def test_run_scope_cleanup_deletes_bindings(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo, is_default=False)
        await repo.create_binding(_binding_payload(execution_scope_id="run-44", session_id=None, scope_kind="run"))
        await repo.create_binding(
            _binding_payload(execution_scope_id="run-44", session_id=None, scope_kind="run", agent_name="delegate")
        )

        deleted = await repo.delete_bindings_by_scope("run-44")
        assert deleted == 2
        assert await repo.list_bindings_by_scope("run-44") == []

    async def test_touch_binding_updates_last_used(self) -> None:
        repo = SandboxRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_session("session-1")
        created = await repo.create_binding(_binding_payload())

        assert await repo.touch_binding(created["id"]) is True
        fetched = await repo.get_binding("session-1", "master")
        assert fetched is not None
        assert fetched["last_used_at"] >= fetched["created_at"]

    # --- agent reference ------------------------------------------------

    async def test_agent_profile_id_roundtrip(self) -> None:
        repo = SandboxRepository(self.session_factory)
        agent_repo = AgentRepository(self.session_factory)
        await self._seed_profile(repo)
        await self._seed_profile(repo, id="profile-node-22", name="Node 22", is_default=False)

        payload = PersistedAgentConfig(
            name="test-agent",
            description="regression test agent",
            system_prompt="You are a test agent.",
            provider=ProviderConfig(
                provider="openai_compatible",
                model="test-model",
                base_url="https://example.com",
            ),
            sandbox_profile_id="profile-node-22",
        ).model_dump(mode="json")
        await agent_repo._save_agents([payload], None)

        agents = await agent_repo._get_agents(None)
        assert len(agents) == 1
        assert agents[0]["sandbox_profile_id"] == "profile-node-22"

        references = await repo.count_profile_references("profile-node-22")
        assert references == {"agents": 1, "instances": 0}

        # PersistedAgentConfig defaults to None (legacy payloads keep working).
        legacy = PersistedAgentConfig(
            name="legacy-agent",
            description="legacy",
            system_prompt="legacy",
            provider=ProviderConfig(provider="openai_compatible", model="m", base_url="https://example.com"),
        )
        assert legacy.sandbox_profile_id is None


if __name__ == "__main__":
    unittest.main()
