"""Sandbox binding application service.

Resolves one logical sandbox binding per ``(execution scope, agent)`` pair:
derives execution/workspace scope ids, resolves the agent's profile (or the
default), pins an immutable spec snapshot on first use, re-evaluates the
agent's outbound policy on every run, and configures the execution backend
without eagerly creating containers. Also owns instance/scope teardown used
by session deletion, stateless-run cleanup, instance reset, and profile
disable.

Implements the runtime ``ExecutionBindingResolver`` protocol structurally;
depends only on core/runtime/infra types (never FastAPI or ``app.state``).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from covalent.application.errors import ConflictError
from covalent.application.services.sandbox_profile_service import (
    EXECUTABLE_STATUSES,
    SandboxProfileService,
)
from covalent.core.agent import AgentSpec
from covalent.core.types import RunContext
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.infra.settings import AppSettings
from covalent.runtime.backend import ExecutionBackend, SandboxBinding, SandboxSpec

logger = logging.getLogger(__name__)


def _dedupe_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _spec_from_snapshot(snapshot: dict[str, Any]) -> SandboxSpec:
    resources = snapshot.get("resources") or {}
    return SandboxSpec(
        profile_id=str(snapshot["profile_id"]),
        profile_revision=int(snapshot.get("profile_revision", 1)),
        image=str(snapshot["image"]),
        pull_policy=str(snapshot.get("pull_policy") or "if_not_present"),
        keepalive_command=tuple(snapshot.get("keepalive_command") or ()),
        runtime_capabilities=frozenset(snapshot.get("runtime_capabilities") or ()),
        contract_version=int(snapshot.get("contract_version", 1)),
        memory_limit=str(resources.get("memory_limit", "")),
        pids_limit=int(resources.get("pids_limit", 0)),
        cpus=float(resources.get("cpus", 0.0)),
        tmpfs_size=str(resources.get("tmpfs_size", "")),
    )


def _snapshot_from_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_id": profile["id"],
        "profile_revision": int(profile.get("revision", 1)),
        "image": profile["image"],
        "image_reference": profile["image"],
        "pull_policy": profile.get("pull_policy") or "if_not_present",
        "keepalive_command": list(profile.get("keepalive_command") or []),
        "runtime_capabilities": list(profile.get("runtime_capabilities") or []),
        "contract_version": int(profile.get("contract_version", 1)),
        "resources": {
            "memory_limit": profile["memory_limit"],
            "pids_limit": int(profile["pids_limit"]),
            "cpus": float(profile["cpus"]),
            "tmpfs_size": profile["tmpfs_size"],
        },
    }


def _binding_from_row(row: dict[str, Any]) -> SandboxBinding:
    from covalent.runtime.backend import ExecutionTarget

    return SandboxBinding(
        target=ExecutionTarget(
            execution_scope_id=row["execution_scope_id"],
            session_id=row["session_id"],
            workspace_scope_id=row["execution_scope_id"],
            sandbox_instance_id=row["id"],
            agent_name=row["agent_name"],
        ),
        spec=_spec_from_snapshot(row["spec_snapshot"]),
        allowed_outbound=tuple(row.get("allowed_outbound_snapshot") or ()),
    )


class SandboxBindingService:
    def __init__(
        self,
        *,
        repository: SandboxRepository,
        profile_service: SandboxProfileService,
        settings: AppSettings,
        execution_backend: ExecutionBackend | Any | None,
        skill_runtime_lookup: Callable[[str], str | None] | None = None,
        process_manager: Any | None = None,
    ) -> None:
        self._repository = repository
        self._profile_service = profile_service
        self._settings = settings
        self._execution_backend = execution_backend
        self._skill_runtime_lookup = skill_runtime_lookup
        self._process_manager = process_manager

    # --- resolver protocol ---------------------------------------------------

    async def resolve(self, agent: AgentSpec, context: RunContext) -> SandboxBinding:
        """Bind this agent run to its logical sandbox. Sets the context's
        execution identity and configures the backend; container creation
        stays lazy (first exec/spawn)."""
        execution_scope_id = (
            context.execution_scope_id
            or (context.session_id if context.memory_mode == "session" else None)
            or (context.metadata.get("run_id") if context.metadata else None)
        )
        if not execution_scope_id:
            raise ConflictError(
                "cannot resolve a sandbox binding without a session id or run id "
                "(execution scope)"
            )
        scope_kind = "session" if context.memory_mode == "session" and context.session_id else "run"
        workspace_scope_id = context.workspace_scope_id or execution_scope_id

        row = await self._repository.get_binding(execution_scope_id, agent.name)
        if row is not None:
            binding = _binding_from_row(row)
            await self._check_profile_revoked(binding)
            binding = await self._reevaluate_outbound(binding, agent)
        else:
            binding = await self._create_binding(
                agent=agent,
                execution_scope_id=execution_scope_id,
                session_id=context.session_id if scope_kind == "session" else None,
                scope_kind=scope_kind,
                workspace_id=self._resolve_workspace_id(context),
            )

        context.execution_scope_id = execution_scope_id
        context.workspace_scope_id = workspace_scope_id
        context.sandbox_instance_id = binding.target.sandbox_instance_id

        if self._execution_backend is not None:
            self._execution_backend.configure(binding)
        await self._repository.touch_binding(binding.target.sandbox_instance_id)
        return binding

    async def _check_profile_revoked(self, binding: SandboxBinding) -> None:
        """Emergency-revocation guard for existing bindings: disabling a profile
        must also block lazy recreation of containers pinned to it. Re-enabling
        the profile permits recreation from the saved snapshot."""
        profile = await self._repository.get_profile(binding.spec.profile_id)
        if profile is None or not profile["enabled"]:
            raise ConflictError(
                f"sandbox profile '{binding.spec.profile_id}' is disabled; "
                "instance recreation is blocked until it is re-enabled"
            )

    async def _reevaluate_outbound(self, binding: SandboxBinding, agent: AgentSpec) -> SandboxBinding:
        """Outbound policy is security policy, not environment identity: it is
        re-evaluated per invocation. A change updates the snapshot and requests
        container recreation with the new network mode."""
        desired = _dedupe_ordered(list(agent.allowed_outbound))
        if list(binding.allowed_outbound) == desired:
            return binding
        updated = await self._repository.update_binding_outbound(
            binding.target.sandbox_instance_id, desired
        )
        if self._execution_backend is not None:
            await self._stop_instance_container(binding.target.sandbox_instance_id)
        if updated is not None:
            return _binding_from_row(updated)
        return binding

    @staticmethod
    def _resolve_workspace_id(context: RunContext) -> str | None:
        if context.workspace_id:
            return context.workspace_id
        principal = context.metadata.get("principal") if context.metadata else None
        if isinstance(principal, dict):
            value = principal.get("workspace_id")
            if isinstance(value, str) and value.strip():
                return value
        return None

    async def _create_binding(
        self,
        *,
        agent: AgentSpec,
        execution_scope_id: str,
        session_id: str | None,
        scope_kind: str,
        workspace_id: str | None = None,
    ) -> SandboxBinding:
        profile = await self._resolve_profile(agent, workspace_id)
        self._validate_profile_usable(agent, profile)

        if scope_kind == "session" and session_id:
            # Console run/stream resolves the binding before the route persists
            # the chat session row (its finally does); the binding's session FK
            # needs the row. Idempotent — the later save upserts full metadata.
            await self._repository.ensure_session_row(session_id, agent_name=agent.name)

        instance_id = f"sbx-{uuid.uuid4().hex[:20]}"
        values = {
            "id": instance_id,
            "execution_scope_id": execution_scope_id,
            "session_id": session_id,
            "scope_kind": scope_kind,
            "agent_name": agent.name,
            "profile_id": profile["id"],
            "profile_name_snapshot": profile["name"],
            "profile_revision": int(profile.get("revision", 1)),
            "spec_snapshot": _snapshot_from_profile(profile),
            "allowed_outbound_snapshot": _dedupe_ordered(list(agent.allowed_outbound)),
        }
        try:
            row = await self._repository.create_binding(values)
        except IntegrityError:
            # A concurrent resolver won the unique (scope, agent) race; load
            # the winner's binding. Correctness relies on the database
            # constraint, not an in-process lock, so this holds across
            # multiple API workers.
            row = await self._repository.get_binding(execution_scope_id, agent.name)
            if row is None:
                raise
        return _binding_from_row(row)

    async def _resolve_profile(self, agent: AgentSpec, workspace_id: str | None = None) -> dict[str, Any]:
        if agent.sandbox_profile_id:
            return await self._profile_service.get_profile(agent.sandbox_profile_id, workspace_id)
        return await self._profile_service.resolve_default_profile(workspace_id)

    def _validate_profile_usable(self, agent: AgentSpec, profile: dict[str, Any]) -> None:
        if not profile["enabled"]:
            raise ConflictError(
                f"agent '{agent.name}' resolves to sandbox profile '{profile['id']}', "
                "which is disabled"
            )
        if profile["validation_status"] not in EXECUTABLE_STATUSES:
            raise ConflictError(
                f"agent '{agent.name}' resolves to sandbox profile '{profile['id']}', "
                f"whose image has not passed validation (status: {profile['validation_status']})"
            )
        if self._skill_runtime_lookup is not None:
            skill_runtimes = {
                skill_name: self._skill_runtime_lookup(skill_name)
                for skill_name in agent.skills
                if self._skill_runtime_lookup(skill_name)
            }
            shell_tool_enabled = (
                self._settings.execution_backend_shell_tool_enabled
                and getattr(self._execution_backend, "name", "") == "docker"
            )
            self._profile_service.check_agent_compatibility(
                agent_name=agent.name,
                profile=profile,
                skill_runtimes=skill_runtimes,
                shell_tool_enabled=shell_tool_enabled,
            )

    # --- teardown ---------------------------------------------------------------

    async def _stop_instance_container(self, sandbox_instance_id: str) -> None:
        # Evict warm skill-process handles first so no handle retains a dead
        # container exec connection.
        if self._process_manager is not None:
            try:
                await self._process_manager.stop_sandbox_instance(sandbox_instance_id)
            except Exception:
                logger.warning(
                    "Failed to evict skill processes for sandbox instance %s",
                    sandbox_instance_id,
                    exc_info=True,
                )
        if self._execution_backend is None:
            return
        stop = getattr(self._execution_backend, "stop_instance", None)
        if stop is None:
            # Compatibility: backends that are not instance-aware yet still
            # expose session-keyed stop.
            legacy_stop = getattr(self._execution_backend, "stop", None)
            if legacy_stop is not None:
                await legacy_stop(sandbox_instance_id)
            return
        await stop(sandbox_instance_id)

    def _remove_scope_state(self, execution_scope_id: str) -> None:
        """Remove instance-private HOME/cache state for every instance of a
        scope. Must run only after all containers and process handles stopped."""
        state_root = self._scope_state_root(execution_scope_id)
        if state_root.exists():
            shutil.rmtree(state_root, ignore_errors=True)

    def _scope_state_root(self, execution_scope_id: str) -> Path:
        safe = "".join(
            char if (char.isalnum() or char in "._-") else "-" for char in execution_scope_id.strip()
        ).strip(".-") or "scope"
        return self._settings.workspace_root() / ".covalent" / "sandbox-state" / safe

    async def stop_session(self, session_id: str) -> list[str]:
        """Stop every active instance bound to a chat session, evict its skill
        processes, and remove instance-private state. Logical binding rows are
        removed by the chat-session FK cascade when the session row is deleted —
        call this BEFORE deleting the session row."""
        stopped: list[str] = []
        scopes: set[str] = set()
        for row in await self._repository.list_bindings_by_session(session_id):
            await self._stop_instance_container(row["id"])
            stopped.append(row["id"])
            scopes.add(str(row["execution_scope_id"]))
        for scope in scopes:
            self._remove_scope_state(scope)
        return stopped

    async def cleanup_run_scope(self, execution_scope_id: str) -> list[str]:
        """Stateless-run cleanup: stop containers and delete every logical
        binding for the run scope. Cancellation-safe to call from ``finally``."""
        stopped: list[str] = []
        for row in await self._repository.list_bindings_by_scope(execution_scope_id):
            await self._stop_instance_container(row["id"])
            stopped.append(row["id"])
        await self._repository.delete_bindings_by_scope(execution_scope_id)
        return stopped

    async def cleanup_stateless_run(self, execution_scope_id: str) -> None:
        """Full cleanup for one ``memory.mode=none`` invocation: containers,
        warm processes, logical bindings, instance-private state, and the run's
        temporary workspace. Cancellation-safe: safe to call repeatedly and from
        ``finally`` paths (client disconnect, timeout, success, failure)."""
        try:
            await self.cleanup_run_scope(execution_scope_id)
        except Exception:
            logger.warning(
                "Stateless run scope cleanup failed for %s", execution_scope_id, exc_info=True
            )
        self._remove_scope_state(execution_scope_id)
        if self._settings.session_workspace_enabled:
            shutil.rmtree(
                self._settings.session_workspace_dir(execution_scope_id), ignore_errors=True
            )

    async def reset_instance(self, sandbox_instance_id: str) -> bool:
        """Stop the container, evict warm processes, remove the instance's
        private state, and delete the logical binding. The next run for this
        (scope, agent) generates a NEW instance id and re-resolves the agent's
        current profile, so leaving the old state dir would orphan it."""
        row = await self._find_binding(sandbox_instance_id)
        if row is None:
            return False
        await self._stop_instance_container(sandbox_instance_id)
        self._remove_instance_state_dir(str(row["execution_scope_id"]), sandbox_instance_id)
        return await self._repository.delete_binding(sandbox_instance_id)

    def _remove_instance_state_dir(self, execution_scope_id: str, sandbox_instance_id: str) -> None:
        safe = "".join(
            char if (char.isalnum() or char in "._-") else "-" for char in sandbox_instance_id.strip()
        ).strip(".-") or "instance"
        instance_state = self._scope_state_root(execution_scope_id) / safe
        if instance_state.exists():
            shutil.rmtree(instance_state, ignore_errors=True)

    async def stop_instance(self, sandbox_instance_id: str) -> bool:
        """Stop the instance's live container (evicting warm skill processes)
        but keep the logical binding, so the next run recreates the container
        from the saved spec. Returns False when no binding exists."""
        row = await self._find_binding(sandbox_instance_id)
        if row is None:
            return False
        await self._stop_instance_container(sandbox_instance_id)
        return True

    async def _find_binding(self, sandbox_instance_id: str) -> dict[str, Any] | None:
        for row in await self._repository.list_all_bindings():
            if row["id"] == sandbox_instance_id:
                return row
        return None

    async def stop_instances_for_profile(self, profile_id: str) -> list[str]:
        """Emergency revocation support: stop every active instance pinned to
        a profile that is being disabled."""
        stopped: list[str] = []
        for row in await self._repository.list_bindings_by_profile(profile_id):
            await self._stop_instance_container(row["id"])
            stopped.append(row["id"])
        return stopped
