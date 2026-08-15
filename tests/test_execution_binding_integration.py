"""Execution binding integration: master/delegate/stateless lifecycle.

Drives the real ``ReactAgentRuntime`` with scripted models and a recording
binding resolver — no Docker, no DB. Validates the runtime wiring added for
per-agent sandbox bindings: distinct instances per agent in one scope, stable
logical bindings across repeat delegations, lazy container creation, and the
delegate context's scope inheritance.
"""

from __future__ import annotations

import unittest
from typing import Any

from covalent.core.types import RunContext
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.backend import ExecutionTarget, SandboxBinding, SandboxSpec
from covalent.runtime.react import ReactAgentRuntime

from tests.helpers import (
    InMemorySessionStore,
    ScriptedModelAdapter,
    make_test_agent,
    text_response,
    tool_call_response,
)


class _RecordingBackend:
    name = "fake"

    def __init__(self) -> None:
        self.configured: list[str] = []
        self.ensure_calls: list[str] = []
        self.exec_calls: list[str] = []

    def configure(self, binding: SandboxBinding) -> None:
        self.configured.append(binding.target.sandbox_instance_id)

    async def ensure(self, sandbox_instance_id: str) -> None:
        self.ensure_calls.append(sandbox_instance_id)

    async def stop_instance(self, sandbox_instance_id: str) -> None:
        pass

    async def exec(self, *args: Any, **kwargs: Any) -> None:
        self.exec_calls.append(str(kwargs))


class _RecordingResolver:
    """Mimics SandboxBindingService: stable instance per (scope, agent), sets
    the context's execution identity, configures the backend (no container)."""

    def __init__(self, backend: _RecordingBackend) -> None:
        self.backend = backend
        self.calls: list[tuple[str, str]] = []
        self._instances: dict[tuple[str, str], str] = {}

    async def resolve(self, agent: Any, context: RunContext) -> SandboxBinding:
        scope = context.execution_scope_id or context.session_id or ""
        self.calls.append((scope, agent.name))
        key = (scope, agent.name)
        instance_id = self._instances.setdefault(key, f"sbx-{agent.name}-{len(self._instances)}")
        context.execution_scope_id = scope
        context.workspace_scope_id = context.workspace_scope_id or scope
        context.sandbox_instance_id = instance_id
        binding = SandboxBinding(
            target=ExecutionTarget(
                execution_scope_id=scope,
                session_id=context.session_id,
                workspace_scope_id=context.workspace_scope_id or scope,
                sandbox_instance_id=instance_id,
                agent_name=agent.name,
            ),
            spec=SandboxSpec(
                profile_id="default",
                profile_revision=1,
                image="covalent-sandbox:dev",
                pull_policy="if_not_present",
                keepalive_command=("tail", "-f", "/dev/null"),
                runtime_capabilities=frozenset({"python", "shell"}),
                contract_version=1,
                memory_limit="512m",
                pids_limit=256,
                cpus=1.0,
                tmpfs_size="128m",
            ),
            allowed_outbound=tuple(agent.allowed_outbound),
        )
        self.backend.configure(binding)
        return binding


def _delegate_runtime(registry: FrameworkRegistry, resolver: _RecordingResolver) -> ReactAgentRuntime:
    return ReactAgentRuntime(
        registry,
        session_store=InMemorySessionStore(),
        session_history_limit=10,
        enable_llm_summarization=False,
        binding_resolver=resolver,
    )


def _registry_with(*agents: Any, model: ScriptedModelAdapter) -> FrameworkRegistry:
    registry = FrameworkRegistry()
    for agent in agents:
        registry.register_agent(agent)
    registry.model_providers[agents[0].provider.cache_key()] = model
    return registry


class ExecutionBindingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, master, worker, *, script):
        model = ScriptedModelAdapter(script)
        backend = _RecordingBackend()
        resolver = _RecordingResolver(backend)
        registry = _registry_with(master, worker, model=model)
        runtime = _delegate_runtime(registry, resolver)
        return runtime, resolver, backend

    async def test_master_and_delegate_get_distinct_instances(self) -> None:
        master = make_test_agent(name="master", description="m", system_prompt="You are the master.")
        master = master.model_copy(update={"delegate_agents": ["worker"]})
        worker = make_test_agent(name="worker", description="w", system_prompt="You are the worker.")
        runtime, resolver, backend = self._run(
            master,
            worker,
            script=[
                tool_call_response("agent__worker", arguments={"input": "do the thing"}, call_id="c1"),
                text_response("worker done"),
                text_response("master final"),
            ],
        )
        context = RunContext(
            agent_name="master",
            session_id="sess-1",
            execution_scope_id="sess-1",
            workspace_scope_id="sess-1",
        )
        response = await runtime.run(master, "Please delegate", context)

        self.assertEqual(response.output_text, "master final")
        self.assertEqual(resolver.calls, [("sess-1", "master"), ("sess-1", "worker")])
        master_instance = context.sandbox_instance_id
        self.assertIsNotNone(master_instance)
        # The delegate resolved its OWN instance, distinct from the master's.
        delegate_instance = [i for i in backend.configured if i != master_instance]
        self.assertEqual(len(delegate_instance), 1)
        # Model-only run: no container creation happened at all.
        self.assertEqual(backend.ensure_calls, [])
        self.assertEqual(backend.exec_calls, [])

    async def test_repeated_delegation_reuses_logical_binding(self) -> None:
        master = make_test_agent(name="master", description="m", system_prompt="m")
        master = master.model_copy(update={"delegate_agents": ["worker"]})
        worker = make_test_agent(name="worker", description="w", system_prompt="w")
        runtime, resolver, backend = self._run(
            master,
            worker,
            script=[
                tool_call_response("agent__worker", arguments={"input": "one"}, call_id="c1"),
                text_response("first answer"),
                tool_call_response("agent__worker", arguments={"input": "two"}, call_id="c2"),
                text_response("second answer"),
                text_response("master final"),
            ],
        )
        context = RunContext(
            agent_name="master", session_id="sess-1", execution_scope_id="sess-1"
        )
        await runtime.run(master, "Delegate twice", context)

        worker_resolves = [call for call in resolver.calls if call[1] == "worker"]
        self.assertEqual(len(worker_resolves), 2)
        # Both worker runs bound to the same stable instance id.
        self.assertEqual(len(backend.configured), 3)  # master + worker + worker
        self.assertEqual(backend.configured[1], backend.configured[2])

    async def test_nested_delegation_shares_scope_per_agent_instances(self) -> None:
        master = make_test_agent(name="master", description="m", system_prompt="m")
        master = master.model_copy(update={"delegate_agents": ["worker"]})
        worker = make_test_agent(name="worker", description="w", system_prompt="w")
        worker = worker.model_copy(update={"delegate_agents": ["sub"]})
        sub = make_test_agent(name="sub", description="s", system_prompt="s")
        runtime, resolver, backend = self._run(
            master,
            worker,
            script=[
                tool_call_response("agent__worker", arguments={"input": "outer"}, call_id="c1"),
                tool_call_response("agent__sub", arguments={"input": "inner"}, call_id="c2"),
                text_response("sub answer"),
                text_response("worker answer"),
                text_response("master final"),
            ],
        )
        # Register the third agent on the same registry.
        runtime.registry.register_agent(sub)

        context = RunContext(
            agent_name="master", session_id="sess-9", execution_scope_id="sess-9"
        )
        await runtime.run(master, "Nested delegation", context)

        self.assertEqual(
            resolver.calls,
            [("sess-9", "master"), ("sess-9", "worker"), ("sess-9", "sub")],
        )
        # Three distinct instances, one scope.
        self.assertEqual(len(set(backend.configured)), 3)

    async def test_stateless_run_scope_binds_per_agent(self) -> None:
        master = make_test_agent(name="master", description="m", system_prompt="m")
        master = master.model_copy(update={"delegate_agents": ["worker"]})
        worker = make_test_agent(name="worker", description="w", system_prompt="w")
        runtime, resolver, backend = self._run(
            master,
            worker,
            script=[
                tool_call_response("agent__worker", arguments={"input": "x"}, call_id="c1"),
                text_response("worker answer"),
                text_response("master final"),
            ],
        )
        context = RunContext(
            agent_name="master",
            session_id=None,
            execution_scope_id="run-42",
            workspace_scope_id="run-42",
            metadata={"memory_mode": "none", "run_id": "run-42"},
        )
        await runtime.run(master, "Stateless", context)

        self.assertEqual(
            resolver.calls, [("run-42", "master"), ("run-42", "worker")]
        )
        self.assertIsNotNone(context.sandbox_instance_id)


class DelegateContextTests(unittest.TestCase):
    def test_delegate_context_inherits_scope_not_instance(self) -> None:
        from covalent.runtime.react import ReactAgentRuntime

        runtime = ReactAgentRuntime.__new__(ReactAgentRuntime)
        parent = make_test_agent(name="master", description="m", system_prompt="m")
        delegate = make_test_agent(name="worker", description="w", system_prompt="w")
        context = RunContext(
            agent_name="master",
            session_id="sess-1",
            execution_scope_id="sess-1",
            workspace_scope_id="sess-1",
            sandbox_instance_id="sbx-master-0",
            metadata={"memory_mode": "session", "delegation_chain": []},
        )
        delegate_context = runtime._build_delegate_context(parent, delegate, context)
        self.assertEqual(delegate_context.session_id, "sess-1")
        self.assertEqual(delegate_context.execution_scope_id, "sess-1")
        self.assertEqual(delegate_context.workspace_scope_id, "sess-1")
        self.assertIsNone(delegate_context.sandbox_instance_id)
        self.assertEqual(delegate_context.metadata["delegated_by"], "master")
        self.assertEqual(delegate_context.metadata["memory_mode"], "session")

    def test_stateless_parent_delegate_context_has_no_session(self) -> None:
        from covalent.runtime.react import ReactAgentRuntime

        runtime = ReactAgentRuntime.__new__(ReactAgentRuntime)
        parent = make_test_agent(name="master", description="m", system_prompt="m")
        delegate = make_test_agent(name="worker", description="w", system_prompt="w")
        context = RunContext(
            agent_name="master",
            session_id=None,
            execution_scope_id="run-42",
            workspace_scope_id="run-42",
            sandbox_instance_id="sbx-master-0",
            metadata={"memory_mode": "none", "run_id": "run-42"},
        )
        delegate_context = runtime._build_delegate_context(parent, delegate, context)
        self.assertIsNone(delegate_context.session_id)
        self.assertEqual(delegate_context.execution_scope_id, "run-42")
        self.assertIsNone(delegate_context.sandbox_instance_id)


if __name__ == "__main__":
    unittest.main()
