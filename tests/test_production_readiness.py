"""Production-readiness tests: resilience + serialization + input validation.

Covers the paths that matter for stability:
- ``BackendUnavailable`` → clean tool errors (not raw exceptions).
- Agent spec serialization preserves ``allowed_outbound``.
- Input validation rejects oversized/malformed payloads.
- Skill-not-executable → typed error.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from agent_framework.api.app import to_agent_summary, _normalize_agent_payload_item
from agent_framework.api.schemas import AgentRunRequest
from agent_framework.core.agent import AgentSpec
from agent_framework.core.types import RunContext, ToolCall
from agent_framework.infra.settings import AppSettings
from agent_framework.model.base import ProviderConfig
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.backend import BackendUnavailable
from agent_framework.skills.process import SkillProcessManager
from agent_framework.skills.spec import ManifestSkillSpec, SkillRuntime

from tests.helpers import make_test_agent, make_test_registry, make_test_runtime, text_response, ScriptedModelAdapter


# ---------------------------------------------------------------------------
# Fake backend that always fails — for BackendUnavailable tests
# ---------------------------------------------------------------------------
class _FailingBackend:
    name = "docker"

    def rewrite_command(self, command):
        return command

    def workspace(self, session_id):
        from agent_framework.runtime.backend import HostPathWorkspace
        from pathlib import Path
        return HostPathWorkspace(host_path=Path("/tmp"))

    def agent_outbound(self, session_id):
        return []

    def record_session(self, *args, **kwargs):
        pass

    async def spawn_stream(self, command, *, cwd=None, env=None, session_id=None):
        raise BackendUnavailable("daemon down", cause=ConnectionError("refused"))

    async def exec(self, command, *, cwd=None, env=None, timeout=None, session_id=None, stdin=None):
        raise BackendUnavailable("daemon down", cause=ConnectionError("refused"))

    async def ensure(self, session_id):
        raise BackendUnavailable("daemon down", cause=ConnectionError("refused"))

    async def stop(self, session_id):
        pass

    async def aclose(self):
        pass


class BackendUnavailableTests(unittest.IsolatedAsyncioTestCase):
    """When the execution backend is unreachable, callers get clean errors."""

    async def test_skill_tool_call_returns_clean_error(self) -> None:
        """_execute_skill_tool catches BackendUnavailable → ToolResult(is_error)."""
        spec = ManifestSkillSpec(
            name="fail-skill",
            description="test",
            runtime=SkillRuntime(type="python", protocol="rpc", entry_point="main.py"),
            source_dir="/tmp",
        )
        registry = FrameworkRegistry()
        registry.register_manifest_skill(spec)
        registry.skill_process_manager = SkillProcessManager(backend=_FailingBackend())
        tool_call = ToolCall(id="tc1", name="some_tool", arguments={})
        ctx = RunContext(agent_name="test", session_id="s1")

        result = await registry._execute_skill_tool("fail-skill", tool_call, ctx)
        self.assertTrue(result.is_error)
        self.assertIn("error", result.content.lower())

    async def test_agent_run_with_failing_backend_continues(self) -> None:
        """A ReAct loop where a tool fails with BackendUnavailable doesn't crash
        the entire run — the agent sees the error and can respond."""
        from tests.helpers import tool_call_response
        # Model calls a tool → tool fails (BackendUnavailable) → model sees error → answers.
        model = ScriptedModelAdapter([
            tool_call_response("failing_tool", arguments={}),
            text_response("The tool is unavailable."),
        ])
        agent = make_test_agent(local_tools=["failing_tool"])

        # Register a tool that raises BackendUnavailable.
        async def _failing_handler(args, ctx):
            raise BackendUnavailable("daemon down")

        registry = make_test_registry(
            agent,
            model=model,
            tools={"failing_tool": ({"type": "function", "function": {"name": "failing_tool", "description": "fails", "parameters": {"type": "object", "properties": {}}}}, _failing_handler)},
        )
        runtime = make_test_runtime(registry)
        response = await runtime.run(agent, "Use the tool", RunContext(agent_name="test", session_id="s1"))
        self.assertIsInstance(response.output_text, str)
        self.assertEqual(model.call_count, 2)  # tool-call turn + error-handling turn


class AgentSerializationTests(unittest.TestCase):
    """Agent config round-trip preserves allowed_outbound."""

    def test_to_agent_summary_includes_allowed_outbound(self) -> None:
        agent = AgentSpec(
            name="test",
            description="test",
            system_prompt="prompt",
            provider=ProviderConfig(provider="test", model="m"),
            allowed_outbound=["api.example.com"],
        )
        summary = to_agent_summary(agent)
        self.assertEqual(summary.allowed_outbound, ["api.example.com"])

    def test_normalize_payload_preserves_allowed_outbound(self) -> None:
        settings = AppSettings()
        item = {
            "name": "test",
            "description": "test",
            "system_prompt": "prompt",
            "provider": {"provider": "test", "model": "m"},
            "allowed_outbound": ["*.bing.com", "api.example.com"],
        }
        normalized = _normalize_agent_payload_item(item, settings)
        self.assertEqual(normalized["allowed_outbound"], ["*.bing.com", "api.example.com"])

    def test_empty_allowed_outbound_normalizes_to_empty(self) -> None:
        settings = AppSettings()
        item = {"name": "test", "description": "d", "system_prompt": "p", "provider": {"provider": "t", "model": "m"}}
        normalized = _normalize_agent_payload_item(item, settings)
        self.assertEqual(normalized["allowed_outbound"], [])


class InputValidationTests(unittest.TestCase):
    """Input validation rejects malformed/oversized payloads."""

    def test_oversized_string_input_rejected(self) -> None:
        """AgentRunRequest with >1M chars → ValidationError."""
        with self.assertRaises(ValidationError):
            AgentRunRequest(input="x" * 1_000_001)

    def test_empty_string_input_rejected(self) -> None:
        """AgentRunRequest with empty string → ValidationError."""
        with self.assertRaises(ValidationError):
            AgentRunRequest(input="")

    def test_empty_content_list_rejected(self) -> None:
        """AgentRunRequest with empty list → ValidationError."""
        with self.assertRaises(ValidationError):
            AgentRunRequest(input=[])


class SkillErrorTests(unittest.IsolatedAsyncioTestCase):
    """Skill-related error conditions produce typed exceptions."""

    async def test_acquire_non_executable_skill_raises(self) -> None:
        """Acquiring a non-executable skill → SkillProcessError."""
        from agent_framework.skills.exceptions import SkillProcessError
        spec = ManifestSkillSpec(name="no-runtime", description="no runtime", source_dir="/tmp")
        spm = SkillProcessManager()
        with self.assertRaises(SkillProcessError):
            await spm.acquire(spec)


if __name__ == "__main__":
    unittest.main()
