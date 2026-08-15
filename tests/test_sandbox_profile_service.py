"""Application-layer tests for the sandbox profile service.

Runs against a real PostgreSQL instance pointed at by ``TEST_DATABASE_URL``
(auto-skip when unset) so the service exercises the real repository paths.
Image validation is injected as a fake port; the real Docker-backed validator
lands with the image-contract task.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from covalent.application.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableEntityError,
)
from covalent.application.schemas import SandboxProfileCreateRequest, SandboxProfileUpdateRequest
from covalent.application.services.sandbox_profile_service import SandboxProfileService
from covalent.infra.db import AgentRow
from covalent.infra.migrations import run_database_migrations
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.infra.settings import AppSettings

_SANDBOX_TABLES = (
    "sandbox_instances, sandbox_profiles, agents, agent_capabilities, "
    "agent_skills, agent_delegates, agent_mcp_servers, agent_mcp_tools, "
    "mcp_servers, mcp_server_env_vars, chat_sessions"
)


class _FakeImageValidator:
    """Stands in for the Docker-backed validation port (Task 7)."""

    def __init__(self, *, result: str = "valid", message: str = "probes passed") -> None:
        self.result = result
        self.message = message
        self.calls: list[dict[str, Any]] = []

    async def validate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(candidate))
        return {
            "status": self.result,
            "image_id": "sha256:abc123",
            "digest": "sha256:abc123",
            "message": self.message,
        }


def _create_request(**overrides: Any) -> SandboxProfileCreateRequest:
    payload: dict[str, Any] = {
        "name": "Python 3.12",
        "image": "covalent-sandbox:py312",
        "keepalive_command": ["tail", "-f", "/dev/null"],
        "runtime_capabilities": ["python", "shell"],
        "memory_limit": "512m",
        "pids_limit": 256,
        "cpus": 1.0,
        "tmpfs_size": "128m",
    }
    payload.update(overrides)
    return SandboxProfileCreateRequest.model_validate(payload)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class SandboxProfileServiceTestCase(unittest.IsolatedAsyncioTestCase):
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

    def _service(
        self,
        settings: AppSettings | None = None,
        validator: _FakeImageValidator | None = None,
    ) -> SandboxProfileService:
        return SandboxProfileService(
            SandboxRepository(self.session_factory),
            settings or AppSettings(),
            image_validator=validator,
        )

    # --- seeding ----------------------------------------------------------

    async def test_seed_creates_legacy_default_from_settings(self) -> None:
        settings = AppSettings(
            execution_backend_docker_image="covalent-sandbox:dev",
            execution_backend_docker_mem_limit="512m",
            execution_backend_docker_pids_limit=256,
            execution_backend_docker_cpus=1.0,
            execution_backend_docker_tmpfs_size="128m",
        )
        service = self._service(settings)
        await service.ensure_seeded()

        profiles = await service.list_profiles()
        assert len(profiles) == 1
        seeded = profiles[0]
        assert seeded["image"] == "covalent-sandbox:dev"
        assert seeded["enabled"] is True
        assert seeded["is_default"] is True
        assert seeded["validation_status"] == "legacy_unverified"
        assert seeded["memory_limit"] == "512m"
        assert "python" in seeded["runtime_capabilities"]

        # Seeding is idempotent and never overwrites existing profiles.
        await service.ensure_seeded()
        assert len(await service.list_profiles()) == 1

    # --- input validation ---------------------------------------------------

    async def test_create_rejects_invalid_input(self) -> None:
        service = self._service()
        cases: list[dict[str, Any]] = [
            {"image": "  "},
            {"keepalive_command": []},
            {"keepalive_command": ["tail", " "]},
            {"memory_limit": "0m"},
            {"memory_limit": "lots"},
            {"pids_limit": 0},
            {"cpus": 0},
            {"cpus": -1.0},
            {"tmpfs_size": "0"},
        ]
        for overrides in cases:
            with self.assertRaises(UnprocessableEntityError, msg=f"expected rejection for {overrides}"):
                await service.create_profile(_create_request(**overrides))

    async def test_create_rejects_unknown_capability(self) -> None:
        service = self._service()
        with self.assertRaises(Exception):
            await service.create_profile(_create_request(runtime_capabilities=["cuda"]))

    async def test_hard_limits_reject_resources_over_max(self) -> None:
        settings = AppSettings(
            execution_backend_docker_max_memory="1g",
            execution_backend_docker_max_cpus=2.0,
            execution_backend_docker_max_pids=512,
        )
        service = self._service(settings)
        with self.assertRaises(UnprocessableEntityError):
            await service.create_profile(_create_request(memory_limit="2g"))
        with self.assertRaises(UnprocessableEntityError):
            await service.create_profile(_create_request(cpus=4.0))
        with self.assertRaises(UnprocessableEntityError):
            await service.create_profile(_create_request(pids_limit=1024))
        # Within limits is accepted.
        created = await service.create_profile(_create_request(memory_limit="512m"))
        assert created["memory_limit"] == "512m"

    async def test_registry_allowlist_uses_parsed_references(self) -> None:
        settings = AppSettings(execution_backend_docker_allowed_image_registries=["ghcr.io"])
        service = self._service(settings)
        with self.assertRaises(UnprocessableEntityError):
            # Substring would match "notghcr.io" — parsing must not.
            await service.create_profile(_create_request(image="notghcr.io/evil/sandbox:latest"))
        allowed = await service.create_profile(_create_request(image="ghcr.io/acme/sandbox:3.12"))
        assert allowed["image"] == "ghcr.io/acme/sandbox:3.12"
        # Unqualified references resolve to docker.io, which is not allowlisted.
        with self.assertRaises(UnprocessableEntityError):
            await service.create_profile(_create_request(image="library/python:3.12"))

    # --- defaults ------------------------------------------------------------

    async def test_new_default_demotes_previous_default_in_scope(self) -> None:
        validator = _FakeImageValidator()
        service = self._service(validator=validator)

        first = await service.create_profile(_create_request(name="first"))
        await service.validate_profile(first["id"])
        await service.enable_profile(first["id"])
        first = await service.update_profile(first["id"], SandboxProfileUpdateRequest(is_default=True))
        assert first["is_default"] is True

        second = await service.create_profile(_create_request(name="second"))
        await service.validate_profile(second["id"])
        await service.enable_profile(second["id"])
        second = await service.update_profile(second["id"], SandboxProfileUpdateRequest(is_default=True))
        assert second["is_default"] is True
        refreshed_first = await service.get_profile(first["id"])
        assert refreshed_first is not None
        assert refreshed_first["is_default"] is False

        # Workspace scope is independent from the global scope.
        workspace_profile = await service.create_profile(
            _create_request(name="ws-default"), workspace_id="ws-1"
        )
        await service.validate_profile(workspace_profile["id"], workspace_id="ws-1")
        await service.enable_profile(workspace_profile["id"], workspace_id="ws-1")
        workspace_profile = await service.update_profile(
            workspace_profile["id"],
            SandboxProfileUpdateRequest(is_default=True),
            workspace_id="ws-1",
        )
        assert workspace_profile["is_default"] is True
        refreshed_second = await service.get_profile(second["id"])
        assert refreshed_second is not None
        assert refreshed_second["is_default"] is True

    # --- lifecycle -------------------------------------------------------------

    async def test_created_profile_starts_disabled_and_pending(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        assert created["enabled"] is False
        assert created["validation_status"] == "pending"

    async def test_enable_requires_validation(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        with self.assertRaises(ConflictError):
            await service.enable_profile(created["id"])

        validator = _FakeImageValidator()
        service_with_validator = self._service(validator=validator)
        validated = await service_with_validator.validate_profile(created["id"])
        assert validated["validation_status"] == "valid"
        assert len(validator.calls) == 1

        enabled = await service_with_validator.enable_profile(created["id"])
        assert enabled["enabled"] is True

    async def test_validate_reports_unavailable_without_port(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        with self.assertRaises(ServiceUnavailableError):
            await service.validate_profile(created["id"])

    async def test_validation_failure_keeps_profile_invalid_and_disabled(self) -> None:
        validator = _FakeImageValidator(result="invalid", message="missing /bin/sh")
        service = self._service(validator=validator)
        created = await service.create_profile(_create_request())
        validated = await service.validate_profile(created["id"])
        assert validated["validation_status"] == "invalid"
        assert validated["validation_message"] == "missing /bin/sh"
        with self.assertRaises(ConflictError):
            await service.enable_profile(created["id"])

    async def test_runtime_affecting_update_validates_candidate_first(self) -> None:
        validator = _FakeImageValidator()
        service = self._service(validator=validator)
        created = await service.create_profile(_create_request())
        await service.validate_profile(created["id"])
        await service.enable_profile(created["id"])

        updated = await service.update_profile(
            created["id"], SandboxProfileUpdateRequest(image="covalent-sandbox:py313")
        )
        assert updated["revision"] == 2
        assert updated["image"] == "covalent-sandbox:py313"
        assert updated["validation_status"] == "valid"
        # One call for the explicit validation, one for the update candidate.
        assert len(validator.calls) == 2
        assert validator.calls[-1]["image"] == "covalent-sandbox:py313"

    async def test_failed_candidate_validation_keeps_active_revision(self) -> None:
        service = self._service(validator=_FakeImageValidator())
        created = await service.create_profile(_create_request())
        await service.validate_profile(created["id"])
        await service.enable_profile(created["id"])

        failing = self._service(validator=_FakeImageValidator(result="invalid", message="no python"))
        with self.assertRaises(ConflictError):
            await failing.update_profile(
                created["id"], SandboxProfileUpdateRequest(image="covalent-sandbox:broken")
            )
        unchanged = await service.get_profile(created["id"])
        assert unchanged is not None
        assert unchanged["image"] == "covalent-sandbox:py312"
        assert unchanged["revision"] == 1

    async def test_enabled_runtime_update_without_validator_is_rejected(self) -> None:
        service = self._service(validator=_FakeImageValidator())
        created = await service.create_profile(_create_request())
        await service.validate_profile(created["id"])
        await service.enable_profile(created["id"])

        with self.assertRaises(ServiceUnavailableError):
            await self._service().update_profile(
                created["id"], SandboxProfileUpdateRequest(image="covalent-sandbox:other")
            )

    async def test_descriptive_update_does_not_bump_revision(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        updated = await service.update_profile(
            created["id"], SandboxProfileUpdateRequest(description="renamed description")
        )
        assert updated["revision"] == 1
        assert updated["description"] == "renamed description"

    async def test_disabled_profile_edit_creates_pending_candidate(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        updated = await service.update_profile(
            created["id"], SandboxProfileUpdateRequest(image="covalent-sandbox:py313")
        )
        assert updated["revision"] == 2
        assert updated["validation_status"] == "pending"
        assert updated["image"] == "covalent-sandbox:py313"

    async def test_disable_and_delete(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        disabled = await service.disable_profile(created["id"])
        assert disabled["enabled"] is False
        assert await service.delete_profile(created["id"]) is True

    async def test_delete_blocked_while_agent_references_profile(self) -> None:
        service = self._service()
        created = await service.create_profile(_create_request())
        async with self.session_factory() as session:
            session.add(AgentRow(name="a1", description="d", system_prompt="s", provider_name="p", provider_model="m", sandbox_profile_id=created["id"]))
            await session.commit()
        with self.assertRaises(ConflictError):
            await service.delete_profile(created["id"])

    # --- visibility & compatibility ---------------------------------------------

    async def test_workspace_visibility_hides_other_workspace_profiles(self) -> None:
        service = self._service()
        hidden = await service.create_profile(_create_request(name="private"), workspace_id="ws-other")
        visible_global = await service.create_profile(_create_request(name="global"))
        visible_ws = await service.create_profile(_create_request(name="mine"), workspace_id="ws-1")

        names = {profile["name"] for profile in await service.list_profiles(workspace_id="ws-1")}
        assert names == {"global", "mine"}

        assert await service.get_profile(visible_global["id"], workspace_id="ws-1") is not None
        assert await service.get_profile(visible_ws["id"], workspace_id="ws-1") is not None
        with self.assertRaises(NotFoundError):
            await service.get_profile(hidden["id"], workspace_id="ws-1")

    async def test_agent_capability_compatibility(self) -> None:
        service = self._service()
        node_profile = await service.create_profile(
            _create_request(name="node", runtime_capabilities=["nodejs"])
        )

        # Python skill requires python capability.
        with self.assertRaises(ConflictError) as ctx:
            service.check_agent_compatibility(
                agent_name="writer",
                profile=node_profile,
                skill_runtimes={"docgen": "python"},
                shell_tool_enabled=False,
            )
        assert "docgen" in str(ctx.exception)
        assert "python" in str(ctx.exception)

        # Shell tool requires shell capability.
        with self.assertRaises(ConflictError):
            service.check_agent_compatibility(
                agent_name="writer",
                profile=node_profile,
                skill_runtimes={"webtool": "nodejs"},
                shell_tool_enabled=True,
            )

        # Compatible configuration passes.
        service.check_agent_compatibility(
            agent_name="writer",
            profile=node_profile,
            skill_runtimes={"webtool": "nodejs"},
            shell_tool_enabled=False,
        )

    async def test_agent_selection_validation_at_save_time(self) -> None:
        validator = _FakeImageValidator()
        service = self._service(validator=validator)
        node_profile = await service.create_profile(_create_request(name="node", runtime_capabilities=["nodejs"]))
        await service.validate_profile(node_profile["id"])
        await service.enable_profile(node_profile["id"])

        def lookup(skill_name: str) -> str | None:
            return {"docgen": "python", "webtool": "nodejs"}.get(skill_name)

        # Unknown profile reference is rejected.
        with self.assertRaises(NotFoundError):
            await service.validate_agent_selections(
                [{"name": "writer", "sandbox_profile_id": "missing", "skills": []}],
                skill_runtime_lookup=lookup,
            )

        # Disabled profile is rejected even when runtimes match.
        disabled = await service.create_profile(_create_request(name="disabled"))
        await service.validate_profile(disabled["id"])
        await service.disable_profile(disabled["id"])
        with self.assertRaises(ConflictError):
            await service.validate_agent_selections(
                [{"name": "writer", "sandbox_profile_id": disabled["id"], "skills": []}],
                skill_runtime_lookup=lookup,
            )

        # Runtime mismatch names the agent, skill, and capability.
        with self.assertRaises(ConflictError) as ctx:
            await service.validate_agent_selections(
                [{"name": "writer", "sandbox_profile_id": node_profile["id"], "skills": ["docgen"]}],
                skill_runtime_lookup=lookup,
            )
        assert "writer" in str(ctx.exception)
        assert "docgen" in str(ctx.exception)

        # Compatible selection and default-selection agents pass.
        await service.validate_agent_selections(
            [
                {"name": "web", "sandbox_profile_id": node_profile["id"], "skills": ["webtool"]},
                {"name": "default-user", "skills": ["docgen"]},
            ],
            skill_runtime_lookup=lookup,
        )

    async def test_legacy_unverified_default_can_be_disabled_and_reenabled(self) -> None:
        service = self._service()
        await service.ensure_seeded()
        profiles = await service.list_profiles()
        seeded = profiles[0]

        disabled = await service.disable_profile(seeded["id"])
        assert disabled["enabled"] is False
        reenabled = await service.enable_profile(seeded["id"])
        assert reenabled["enabled"] is True


if __name__ == "__main__":
    unittest.main()
