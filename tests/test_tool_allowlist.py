"""Execution-side tool allowlist enforcement.

A session can switch agents mid-history (agent A with MCP tool 1, later agent B
without it). B sees tool-1 calls in the transcript and may imitate them; the
runtime must deny any tool call that was not exposed to the CURRENT agent in
THIS request, instead of letting the registry execute globally-registered tools.
"""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from typing import Any

from covalent.core.types import RunContext
from covalent.mcp.spec import McpServerConfig, McpToolReference
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.delegation import DelegateRunHandle
from covalent.runtime.react import ReactAgentRuntime

from tests.helpers import (
    ScriptedModelAdapter,
    make_test_agent,
    make_test_registry,
    make_test_runtime,
    text_response,
    tool_call_response,
)


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _tool_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Test tool {name}.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _RecordingMcpClient:
    def __init__(self, tools: list[McpToolReference]) -> None:
        self._tools = tools
        self.calls: list[tuple[str, str]] = []

    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        return self._tools

    async def call_tool(self, server: McpServerConfig, tool_name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((server.name, tool_name))
        return SimpleNamespace(
            structuredContent=None,
            content=[SimpleNamespace(text="mcp-ok")],
            isError=False,
        )


class McpToolOfAnotherAgentIsDenied(unittest.IsolatedAsyncioTestCase):
    async def test_history_imitated_mcp_call_never_reaches_the_server(self) -> None:
        """The core bug: agent B (no mcp_tools) imitates agent A's MCP tool call
        from session history. The allowlist must deny it before the MCP client
        is invoked."""
        mcp_client = _RecordingMcpClient([
            McpToolReference(server_name="query-server", tool_name="tool1", input_schema={}),
        ])
        agent = make_test_agent(name="agent-b", local_tools=[])
        model = ScriptedModelAdapter([
            tool_call_response("mcp__query-server__tool1", arguments={}, call_id="c-1"),
            text_response("Done"),
        ])
        registry = make_test_registry(agent, model=model)
        registry.register_mcp_server(McpServerConfig(name="query-server", transport="streamable_http", url="http://x/mcp"))
        registry.set_mcp_client(mcp_client)
        runtime = make_test_runtime(registry)

        final = await runtime.run(agent, "go", RunContext(agent_name="agent-b", session_id="s1"))

        self.assertEqual(final.output_text, "Done")
        self.assertEqual(mcp_client.calls, [], "denied tool must never reach the MCP server")


class UnexposedLocalToolIsDenied(unittest.IsolatedAsyncioTestCase):
    async def test_globally_registered_local_tool_not_in_agent_config_is_denied(self) -> None:
        handler_calls: list[str] = []

        async def _handler(args: dict[str, Any], ctx: RunContext | None) -> str:
            handler_calls.append("invoked")
            return "secret result"

        agent = make_test_agent(name="agent-b", local_tools=[])
        model = ScriptedModelAdapter([
            tool_call_response("secret_tool", arguments={}, call_id="c-1"),
            text_response("Done"),
        ])
        registry = make_test_registry(agent, model=model, tools={"secret_tool": (_tool_schema("secret_tool"), _handler)})
        runtime = make_test_runtime(registry)

        final = await runtime.run(agent, "go", RunContext(agent_name="agent-b", session_id="s1"))

        self.assertEqual(final.output_text, "Done")
        self.assertEqual(handler_calls, [])


class AllowedToolStillExecutes(unittest.IsolatedAsyncioTestCase):
    async def test_agent_declared_local_tool_executes_normally(self) -> None:
        async def _handler(args: dict[str, Any], ctx: RunContext | None) -> str:
            return "ok result"

        agent = make_test_agent(name="agent-b", local_tools=["ok_tool"])
        model = ScriptedModelAdapter([
            tool_call_response("ok_tool", arguments={}, call_id="c-1"),
            text_response("Done"),
        ])
        registry = make_test_registry(agent, model=model, tools={"ok_tool": (_tool_schema("ok_tool"), _handler)})
        runtime = make_test_runtime(registry)

        final = await runtime.run(agent, "go", RunContext(agent_name="agent-b", session_id="s1"))

        self.assertEqual(final.output_text, "Done")
        # The tool result actually came from the handler, not from a denial.
        self.assertNotIn("not available", json.dumps(final.model_dump()))


class DoubleEncodedNameResolution(unittest.TestCase):
    def test_fuzzy_resolves_double_encoded_name_to_allowed_canonical(self) -> None:
        """A double-encoded echo of an ALLOWED tool resolves via fuzzy match, so
        the allowlist check must not kill it (non-compliant segments are b64)."""
        agent = make_test_agent(name="agent-b")
        registry = make_test_registry(agent)
        registry.register_mcp_server(McpServerConfig(name="my.server", transport="streamable_http", url="http://x/mcp"))
        canonical = f"mcp__{_b64('my.server')}__{_b64('获取本体')}"
        echo = f"mcp__{_b64('my.server')}__mcp__{_b64('my.server')}__{_b64('获取本体')}"
        self.assertEqual(registry.fuzzy_match_tool_name(echo), canonical)


class LifecycleStringifyGuardStillCovered(unittest.IsolatedAsyncioTestCase):
    async def test_handle_reaching_generic_path_is_never_stringified(self) -> None:
        """The allowlist denies unexposed lifecycle tools upstream (see
        test_agent_react.py); this keeps unit coverage on the generic-path
        guard for a mis-wired assembly where the tool WAS exposed."""
        async def _handler(args: dict[str, Any], ctx: RunContext | None) -> DelegateRunHandle:
            return DelegateRunHandle(
                run=None, agent=agent, context=RunContext(agent_name="parent"), initial_input=""
            )

        agent = make_test_agent(name="parent", local_tools=[])
        registry = make_test_registry(agent, tools={"delegate_send": (_tool_schema("delegate_send"), _handler)})
        runtime = make_test_runtime(registry)

        from covalent.core.types import ToolCall

        results = await runtime._execute_tool_calls(
            agent,
            [ToolCall(id="c-1", name="delegate_send", arguments={})],
            iteration=1,
            context=RunContext(agent_name="parent"),
            allowed_tool_names={"delegate_send"},
        )

        self.assertTrue(results[0].is_error)
        self.assertEqual(
            str(results[0].content),
            "delegate lifecycle tool executed outside the stateful runtime",
        )


if __name__ == "__main__":
    unittest.main()
