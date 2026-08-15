"""Application-layer tests for the sandbox binding service.

Runs against a real PostgreSQL instance pointed at by ``TEST_DATABASE_URL``
(auto-skip when unset). The execution backend is a fake recording configure /
stop calls; the resolver contract and Docker rekey land in later tasks.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

from sqlalchemy import pool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.application.errors import ConflictError, NotFoundError
from covalent.application.schemas import SandboxProfileUpdateRequest
from covalent.application.services.sandbox_binding_service import SandboxBindingService
from covalent.application.services.sandbox_profile_service import SandboxProfileService
from covalent.core.agent import AgentSpec
from covalent.core.types import RunContext
from covalent.infra.db import ChatSessionRow
from covalent.infra.migrations import run_database_migrations
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.infra.settings import AppSettings
from covalent.model.base import ProviderConfig
from covalent.runtime.backend import SandboxBinding

_SANDBOX_TABLES = (
    "sandbox_instances, sandbox_profiles, agents, agent_capabilities, "
    "agent_skills, agent_delegates, agent_mcp_servers, agent_mcp_tools, "
    "mcp_servers, mcp_server_env_vars, chat_sessions"
)


class _FakeImageValidator:
    async def validate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {"status": "valid", "image_id": "sha256:x", "digest": "sha256:x", "message": "ok"}


class _FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.configured: list[SandboxBinding] = []
        self.stopped_instances: list[str] = []

    def configure(self, binding: SandboxBinding) -> None:
        self.configured.append(binding)

    async def stop_instance(self, sandbox_instance_id: str) -> None:
        self.stopped_instances.append(sandbox_instance_id)


def _agent(name: str = "master", **overrides: Any) -> AgentSpec:
    payload: dict[str, Any] = {
        "name": name,
        "description": "test agent",
        "system_prompt": "You are a test agent.",
        "provider": ProviderConfig(provider="openai_compatible", model="m", base_url="https://example.com"),
    }
    payload.update(overrides)
    return AgentSpec.model_validate(payload)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class SandboxBindingServiceTestCase(unittest.IsolatedAsyncioTestCase):
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

    def _build(
        self,
        backend: _FakeBackend | None = None,
        *,
        skill_runtimes: dict[str, str] | None = None,
    ) -> tuple[SandboxBindingService, SandboxRepository]:
        repository = SandboxRepository(self.session_factory)
        profile_service = SandboxProfileService(
            repository, AppSettings(), image_validator=_FakeImageValidator()
        )
        backend = backend or _FakeBackend()
        service = SandboxBindingService(
            repository=repository,
            profile_service=profile_service,
            settings=AppSettings(),
            execution_backend=backend,
            skill_runtime_lookup=(skill_runtimes or {}).get,
        )
        return service, repository

    async def _seed_session(self, session_id: str) -> None:
        async with self.session_factory() as session:
            session.add(ChatSessionRow(id=session_id, agent_name="master"))
            await session.commit()

    def _context(self, session_id: str | None = "session-1", **overrides: Any) -> RunContext:
        return RunContext(agent_name="master", session_id=session_id, **overrides)

    # --- resolution ------------------------------------------------------

    async def test_resolve_ensures_chat_session_row_for_new_console_session(self) -> None:
        """Console runs start the runtime before the chat_sessions row exists
        (the stream persists it in its finally) — the binding path must ensure
        the row itself, or the session-scoped FK fails before the model runs."""
        service, _ = self._build()
        # NOTE: no _seed_session("session-new") — brand new console session.
        context = self._context(session_id="session-new")
        binding = await service.resolve(_agent("master"), context)

        self.assertEqual(binding.target.session_id, "session-new")
        async with self.session_factory() as session:
            row = await session.get(ChatSessionRow, "session-new")
            self.assertIsNotNone(row, "binding resolve must ensure the chat session row exists")

    async def test_stateless_resolve_never_creates_chat_session(self) -> None:
        service, _ = self._build()
        context = RunContext(
            agent_name="master",
            session_id=None,
            execution_scope_id="run-77",
            workspace_scope_id="run-77",
            metadata={"memory_mode": "none"},
        )
        await service.resolve(_agent("master"), context)
        async with self.session_factory() as session:
            row = await session.get(ChatSessionRow, "run-77")
            self.assertIsNone(row, "stateless runs must never create a fake chat session")

    async def test_master_and_delegate_get_distinct_bindings(self) -> None:
        service, _ = self._build()
        await self._seed_session("session-1")

        master_ctx = self._context()
        master_binding = await service.resolve(_agent("master"), master_ctx)
        delegate_ctx = self._context()
        delegate_binding = await service.resolve(_agent("delegate"), delegate_ctx)

        assert master_binding.target.sandbox_instance_id != delegate_binding.target.sandbox_instance_id
        assert master_binding.target.execution_scope_id == "session-1"
        assert delegate_binding.target.execution_scope_id == "session-1"
        assert master_binding.target.workspace_scope_id == "session-1"

        assert master_ctx.sandbox_instance_id == master_binding.target.sandbox_instance_id
        assert master_ctx.execution_scope_id == "session-1"
        assert master_ctx.workspace_scope_id == "session-1"
        assert delegate_ctx.sandbox_instance_id == delegate_binding.target.sandbox_instance_id

        # Both bindings are registered with the backend without creating containers.
        assert len({b.target.sandbox_instance_id for b in [_backend_of(service).configured[0], _backend_of(service).configured[1]]}) == 2

    async def test_same_agent_reuses_logical_binding(self) -> None:
        service, _ = self._build()
        await self._seed_session("session-1")

        first = await service.resolve(_agent("master"), self._context())
        second = await service.resolve(_agent("master"), self._context())
        assert first.target.sandbox_instance_id == second.target.sandbox_instance_id

    async def test_existing_binding_keeps_pinned_spec_after_profile_edit(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        first = await service.resolve(_agent("master"), self._context())
        original_image = first.spec.image

        # Bump the profile revision out from under the binding.
        await repository.update_profile(
            first.spec.profile_id, {"revision": first.spec.profile_revision + 1, "image": "covalent-sandbox:changed"}
        )

        second = await service.resolve(_agent("master"), self._context())
        assert second.target.sandbox_instance_id == first.target.sandbox_instance_id
        assert second.spec.image == original_image
        assert second.spec.profile_revision == first.spec.profile_revision

    async def test_outbound_change_updates_snapshot_and_requests_recreation(self) -> None:
        backend = _FakeBackend()
        service, _ = self._build(backend)
        await self._seed_session("session-1")

        first = await service.resolve(_agent("master", allowed_outbound=[]), self._context())
        assert first.allowed_outbound == ()

        second = await service.resolve(
            _agent("master", allowed_outbound=["api.example.com"]), self._context()
        )
        assert second.target.sandbox_instance_id == first.target.sandbox_instance_id
        assert second.allowed_outbound == ("api.example.com",)
        # Recreation was requested for exactly this instance; the profile
        # snapshot stayed put.
        assert backend.stopped_instances == [first.target.sandbox_instance_id]
        assert second.spec == first.spec

    async def test_stateless_run_scope_binds_without_chat_session(self) -> None:
        service, _ = self._build()
        context = RunContext(
            agent_name="master",
            session_id=None,
            execution_scope_id="run-42",
            workspace_scope_id="run-42",
            metadata={"memory_mode": "none"},
        )
        binding = await service.resolve(_agent("master"), context)
        assert binding.target.execution_scope_id == "run-42"
        assert binding.target.session_id is None
        assert context.sandbox_instance_id == binding.target.sandbox_instance_id

    async def test_stateless_scope_requires_execution_scope(self) -> None:
        service, _ = self._build()
        context = RunContext(agent_name="master", session_id=None)
        with self.assertRaises(ConflictError):
            await service.resolve(_agent("master"), context)

    async def test_concurrent_create_returns_winner_binding(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        winner = await service.resolve(_agent("master"), self._context())

        racing = _RacingRepository(repository, winner)
        profile_service = SandboxProfileService(
            racing, AppSettings(), image_validator=_FakeImageValidator()
        )
        racing_service = SandboxBindingService(
            repository=racing,
            profile_service=profile_service,
            settings=AppSettings(),
            execution_backend=_FakeBackend(),
            skill_runtime_lookup=None,
        )
        loser_binding = await racing_service.resolve(_agent("master"), self._context())
        assert loser_binding.target.sandbox_instance_id == winner.target.sandbox_instance_id

    # --- validation --------------------------------------------------------

    async def test_workspace_owned_profile_resolves_with_workspace_context(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        profile_service = service._profile_service
        ws_profile = await profile_service.create_profile(
            _profile_create("ws-node", runtime=["nodejs"]), workspace_id="ws-1"
        )
        await profile_service.validate_profile(ws_profile["id"], workspace_id="ws-1")
        await profile_service.enable_profile(ws_profile["id"], workspace_id="ws-1")

        # Without workspace context the workspace-owned profile is invisible.
        agent = _agent("master", sandbox_profile_id=ws_profile["id"])
        with self.assertRaises(NotFoundError):
            await service.resolve(agent, self._context())

        # With matching workspace context it resolves.
        context = self._context()
        context.workspace_id = "ws-1"
        binding = await service.resolve(agent, context)
        self.assertEqual(binding.spec.profile_id, ws_profile["id"])

    async def test_default_profile_prefers_workspace_scope(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        profile_service = service._profile_service

        # Global default (seeded) + a workspace-owned default for ws-1.
        await profile_service.ensure_seeded()
        ws_default = await profile_service.create_profile(
            _profile_create("ws-default", runtime=["nodejs"]), workspace_id="ws-1"
        )
        await profile_service.validate_profile(ws_default["id"], workspace_id="ws-1")
        await profile_service.enable_profile(ws_default["id"], workspace_id="ws-1")
        await profile_service.update_profile(
            ws_default["id"],
            SandboxProfileUpdateRequest(is_default=True),
            workspace_id="ws-1",
        )

        # A workspace-context agent without an explicit profile gets the
        # workspace-owned default, not the cross-workspace global one.
        context = self._context()
        context.workspace_id = "ws-1"
        binding = await service.resolve(_agent("master"), context)
        self.assertEqual(binding.spec.profile_id, ws_default["id"])

    async def test_resolve_rejects_disabled_profile(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        profile = await service._profile_service.create_profile(
            _profile_create("node", runtime=["nodejs"])
        )
        # Profile stays disabled (never enabled); agent selects it explicitly.
        with self.assertRaises(ConflictError):
            await service.resolve(
                _agent("master", sandbox_profile_id=profile["id"]), self._context()
            )

    async def test_resolve_validates_skill_runtimes(self) -> None:
        service, repository = self._build()
        await self._seed_session("session-1")
        profile_service = service._profile_service
        node_profile = await profile_service.create_profile(
            _profile_create("node", runtime=["nodejs"])
        )
        await profile_service.validate_profile(node_profile["id"])
        await profile_service.enable_profile(node_profile["id"])

        build = self._build(skill_runtimes={"docgen": "python"})
        build_service = SandboxBindingService(
            repository=SandboxRepository(self.session_factory),
            profile_service=build[0]._profile_service,
            settings=AppSettings(),
            execution_backend=_FakeBackend(),
            skill_runtime_lookup={"docgen": "python"}.get,
        )
        with self.assertRaises(ConflictError) as ctx:
            await build_service.resolve(
                _agent("master", sandbox_profile_id=node_profile["id"], skills=["docgen"]),
                self._context(),
            )
        assert "docgen" in str(ctx.exception)

    # --- lifecycle ----------------------------------------------------------

    async def test_stop_session_stops_all_instances(self) -> None:
        backend = _FakeBackend()
        service, _ = self._build(backend)
        await self._seed_session("session-1")
        master = await service.resolve(_agent("master"), self._context())
        delegate = await service.resolve(_agent("delegate"), self._context())

        await service.stop_session("session-1")
        assert set(backend.stopped_instances) == {
            master.target.sandbox_instance_id,
            delegate.target.sandbox_instance_id,
        }

    async def test_cleanup_run_scope_stops_and_deletes_bindings(self) -> None:
        backend = _FakeBackend()
        service, repository = self._build(backend)
        for agent_name in ("master", "delegate"):
            context = RunContext(
                agent_name=agent_name,
                session_id=None,
                execution_scope_id="run-42",
                workspace_scope_id="run-42",
            )
            await service.resolve(_agent(agent_name), context)

        await service.cleanup_run_scope("run-42")
        assert len(backend.stopped_instances) == 2
        assert await repository.list_bindings_by_scope("run-42") == []

    async def test_reset_instance_stops_container_and_deletes_binding(self) -> None:
        backend = _FakeBackend()
        service, repository = self._build(backend)
        await self._seed_session("session-1")
        binding = await service.resolve(_agent("master"), self._context())

        await service.reset_instance(binding.target.sandbox_instance_id)
        assert backend.stopped_instances == [binding.target.sandbox_instance_id]
        assert await repository.get_binding("session-1", "master") is None

    async def test_profile_disable_stops_its_instances(self) -> None:
        backend = _FakeBackend()
        service, repository = self._build(backend)
        await self._seed_session("session-1")
        binding = await service.resolve(_agent("master"), self._context())

        stopped = await service.stop_instances_for_profile(binding.spec.profile_id)
        assert stopped == [binding.target.sandbox_instance_id]
        assert backend.stopped_instances == [binding.target.sandbox_instance_id]


def _backend_of(service: SandboxBindingService) -> _FakeBackend:
    return service._execution_backend  # type: ignore[return-value]


def _profile_create(name: str, *, runtime: list[str]) -> Any:
    from covalent.application.schemas import SandboxProfileCreateRequest

    return SandboxProfileCreateRequest(
        name=name,
        image="covalent-sandbox:dev",
        keepalive_command=["tail", "-f", "/dev/null"],
        runtime_capabilities=runtime,
        memory_limit="512m",
        pids_limit=256,
        cpus=1.0,
        tmpfs_size="128m",
    )


class _RacingRepository(SandboxRepository):
    """Simulates a concurrent winner committing between get and create."""

    def __init__(self, inner: SandboxRepository, winner: dict[str, Any]) -> None:
        super().__init__(inner._session_factory)
        self._inner = inner
        self._winner = winner
        self._get_calls = 0

    async def get_binding(self, execution_scope_id: str, agent_name: str) -> dict[str, Any] | None:
        self._get_calls += 1
        if self._get_calls == 1:
            return None  # Race: winner has not committed yet.
        return await self._inner.get_binding(execution_scope_id, agent_name)

    async def create_binding(self, values: dict[str, Any]) -> dict[str, Any]:
        raise IntegrityError("duplicate key", None, Exception("unique violation"))


if __name__ == "__main__":
    unittest.main()
