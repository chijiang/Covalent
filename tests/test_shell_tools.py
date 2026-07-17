"""Unit tests for the sandbox shell tool — gating logic (no daemon required).

The shell tool must be registered ONLY on non-filesystem backends AND only when
``execution_backend_shell_tool_enabled`` is set. These tests pin that gate.
"""

from __future__ import annotations

import types
import unittest

from agent_framework.core.shell_tools import RUN_SHELL_TOOL, register_shell_tool, shell_tool_available
from agent_framework.infra.settings import AppSettings


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


if __name__ == "__main__":
    unittest.main()
