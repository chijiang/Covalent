from __future__ import annotations

import dataclasses
import unittest

from covalent.core.types import RunContext
from covalent.runtime.backend import ExecutionTarget, SandboxBinding, SandboxSpec
from covalent.runtime.filesystem_backend import FileSystemBackend


def _sample_spec() -> SandboxSpec:
    return SandboxSpec(
        profile_id="profile-python-312",
        profile_revision=3,
        image="covalent-sandbox:dev",
        keepalive_command=("tail", "-f", "/dev/null"),
        runtime_capabilities=frozenset({"python", "shell"}),
        contract_version=1,
        memory_limit="512m",
        pids_limit=256,
        cpus=1.0,
        tmpfs_size="128m",
    )


def _sample_target() -> ExecutionTarget:
    return ExecutionTarget(
        execution_scope_id="session-1",
        session_id="session-1",
        workspace_scope_id="session-1",
        sandbox_instance_id="sbx-abc123",
        agent_name="master",
    )


def _sample_binding() -> SandboxBinding:
    return SandboxBinding(
        target=_sample_target(),
        spec=_sample_spec(),
        allowed_outbound=("api.example.com",),
    )


class ExecutionValueObjectTests(unittest.TestCase):
    """Execution identity value objects: value equality, immutability."""

    def test_execution_target_equal_by_value(self) -> None:
        self.assertEqual(_sample_target(), _sample_target())
        self.assertNotEqual(
            _sample_target(),
            dataclasses.replace(_sample_target(), agent_name="delegate"),
        )

    def test_execution_target_is_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _sample_target().agent_name = "other"  # type: ignore[misc]

    def test_execution_target_allows_stateless_session(self) -> None:
        target = dataclasses.replace(
            _sample_target(), session_id=None, execution_scope_id="run-42"
        )
        self.assertIsNone(target.session_id)
        self.assertEqual(target.execution_scope_id, "run-42")

    def test_sandbox_spec_equal_by_value(self) -> None:
        self.assertEqual(_sample_spec(), _sample_spec())
        self.assertNotEqual(
            _sample_spec(),
            dataclasses.replace(_sample_spec(), image="covalent-sandbox:other"),
        )

    def test_sandbox_spec_is_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _sample_spec().image = "other"  # type: ignore[misc]

    def test_sandbox_binding_groups_target_spec_and_outbound(self) -> None:
        binding = _sample_binding()
        self.assertEqual(binding, _sample_binding())
        self.assertEqual(binding.target.agent_name, "master")
        self.assertEqual(binding.spec.profile_id, "profile-python-312")
        self.assertEqual(binding.allowed_outbound, ("api.example.com",))

    def test_sandbox_binding_is_frozen(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            _sample_binding().allowed_outbound = ()  # type: ignore[misc]


class RunContextExecutionIdentityTests(unittest.TestCase):
    """RunContext gains optional execution identity without breaking legacy use."""

    def test_execution_identity_defaults_to_none(self) -> None:
        context = RunContext(agent_name="master")
        self.assertIsNone(context.execution_scope_id)
        self.assertIsNone(context.workspace_scope_id)
        self.assertIsNone(context.sandbox_instance_id)

    def test_execution_identity_round_trips_through_copy_and_dump(self) -> None:
        context = RunContext(
            agent_name="master",
            session_id="session-1",
            execution_scope_id="session-1",
            workspace_scope_id="session-1",
            sandbox_instance_id="sbx-abc123",
        )
        copied = context.model_copy()
        self.assertEqual(copied.execution_scope_id, "session-1")
        self.assertEqual(copied.workspace_scope_id, "session-1")
        self.assertEqual(copied.sandbox_instance_id, "sbx-abc123")

        dumped = context.model_dump()
        restored = RunContext.model_validate(dumped)
        self.assertEqual(restored.execution_scope_id, "session-1")
        self.assertEqual(restored.workspace_scope_id, "session-1")
        self.assertEqual(restored.sandbox_instance_id, "sbx-abc123")

    def test_stateless_run_keeps_session_none_with_execution_scope(self) -> None:
        context = RunContext(
            agent_name="master",
            session_id=None,
            execution_scope_id="run-42",
            workspace_scope_id="run-42",
        )
        self.assertIsNone(context.session_id)
        self.assertEqual(context.execution_scope_id, "run-42")

    def test_legacy_construction_remains_valid(self) -> None:
        context = RunContext(agent_name="master", session_id="session-1")
        self.assertEqual(context.session_id, "session-1")
        self.assertEqual(context.memory_mode, "session")


class FileSystemBackendConfigureTests(unittest.TestCase):
    """FileSystemBackend.configure() accepts a binding as a no-op."""

    def test_configure_is_no_op(self) -> None:
        backend = FileSystemBackend()
        self.assertIsNone(backend.configure(_sample_binding()))


if __name__ == "__main__":
    unittest.main()
