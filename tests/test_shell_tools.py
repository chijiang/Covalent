"""Unit tests for the sandbox shell tool — gating logic (no daemon required).

The shell tool must be registered ONLY on non-filesystem backends AND only when
``execution_backend_shell_tool_enabled`` is set. These tests pin that gate.
"""

from __future__ import annotations

import json
import types
import unittest

from agent_framework.core.agent import AgentSpec
from agent_framework.core.shell_tools import RUN_SHELL_TOOL, register_shell_tool, shell_tool_available
from agent_framework.infra.settings import AppSettings
from agent_framework.model.base import ProviderConfig
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.backend import BackendUnavailable


def _agent(name: str = "a", local_tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="test",
        system_prompt="test",
        provider=ProviderConfig(provider="openai_compatible", model="m"),
        local_tools=list(local_tools or []),
    )


class _FakeRegistry:
    def __init__(self) -> None:
        self.local_tools: dict[str, object] = {}

    def register_local_tool(self, name: str, schema: dict, handler=None) -> None:
        self.local_tools[name] = handler


class _FakeBackend:
    def __init__(self, name: str) -> None:
        self.name = name


class ShellToolGatingTests(unittest.TestCase):
    def test_available_false_on_filesystem_even_if_enabled(self) -> None:
        settings = AppSettings(execution_backend_shell_tool_enabled=True)
        self.assertFalse(shell_tool_available(settings, _FakeBackend("filesystem")))

    def test_available_true_on_docker_when_enabled(self) -> None:
        settings = AppSettings(execution_backend_shell_tool_enabled=True)
        self.assertTrue(shell_tool_available(settings, _FakeBackend("docker")))

    def test_available_false_when_disabled(self) -> None:
        settings = AppSettings(execution_backend_shell_tool_enabled=False)
        self.assertFalse(shell_tool_available(settings, _FakeBackend("docker")))

    def test_available_false_when_backend_none(self) -> None:
        settings = AppSettings(execution_backend_shell_tool_enabled=True)
        self.assertFalse(shell_tool_available(settings, None))

    def test_not_registered_on_filesystem(self) -> None:
        registry = _FakeRegistry()
        register_shell_tool(registry, AppSettings(execution_backend_shell_tool_enabled=True), _FakeBackend("filesystem"))
        self.assertNotIn(RUN_SHELL_TOOL, registry.local_tools)

    def test_registered_on_docker_when_enabled(self) -> None:
        registry = _FakeRegistry()
        register_shell_tool(registry, AppSettings(execution_backend_shell_tool_enabled=True), _FakeBackend("docker"))
        self.assertIn(RUN_SHELL_TOOL, registry.local_tools)


class _UnavailableBackend:
    name = "docker"

    def rewrite_command(self, command):
        return command

    async def exec(self, *args, **kwargs):
        raise BackendUnavailable("daemon down", cause=ConnectionError("refused"))


class ShellToolResolutionTests(unittest.IsolatedAsyncioTestCase):
    """run_shell is opt-in per agent: an agent only gets it when its local_tools
    lists it AND the tool is registered (sandbox backend + flag on). Not registered
    -> never exposed, even if listed."""

    async def test_exposed_when_listed_and_registered(self) -> None:
        registry = FrameworkRegistry()
        register_shell_tool(registry, AppSettings(execution_backend_shell_tool_enabled=True), _FakeBackend("docker"))
        tools = await registry.resolve_tools_for_agent(_agent(local_tools=[RUN_SHELL_TOOL]))
        names = {t["function"]["name"] for t in tools}
        self.assertIn(RUN_SHELL_TOOL, names)

    async def test_not_exposed_when_registered_but_not_listed(self) -> None:
        registry = FrameworkRegistry()
        register_shell_tool(registry, AppSettings(execution_backend_shell_tool_enabled=True), _FakeBackend("docker"))
        tools = await registry.resolve_tools_for_agent(_agent())  # local_tools=[]
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn(RUN_SHELL_TOOL, names)

    async def test_not_exposed_when_not_registered_even_if_listed(self) -> None:
        registry = FrameworkRegistry()  # run_shell NOT registered (flag off / fs)
        tools = await registry.resolve_tools_for_agent(_agent(local_tools=[RUN_SHELL_TOOL]))
        names = {t["function"]["name"] for t in tools}
        self.assertNotIn(RUN_SHELL_TOOL, names)

    async def test_run_shell_returns_clean_error_when_backend_unavailable(self) -> None:
        tools: dict[str, object] = {}
        registry = types.SimpleNamespace(
            register_local_tool=lambda name, schema, handler=None: tools.__setitem__(name, handler)
        )
        register_shell_tool(registry, AppSettings(execution_backend_shell_tool_enabled=True), _UnavailableBackend())
        ctx = types.SimpleNamespace(session_id="s1")
        result = json.loads(await tools[RUN_SHELL_TOOL]({"command": "echo hi"}, ctx))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], -1)
        self.assertEqual(result["execution_backend"], "docker")
        self.assertIn("sandbox unavailable", result["error"])


if __name__ == "__main__":
    unittest.main()
