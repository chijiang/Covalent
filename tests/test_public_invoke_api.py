from __future__ import annotations

import json
import unittest

from fastapi import HTTPException

from agent_framework.api.app import _normalize_token_policy, _normalize_token_scopes, _public_stream_events
from agent_framework.api.auth import (
    ApiPrincipal,
    generate_api_token,
    hash_api_token,
    require_agent_allowed,
    require_memory_mode_allowed,
    require_scope,
    require_trace_level_allowed,
)


class ApiTokenPolicyTests(unittest.TestCase):
    def test_normalize_token_scopes_dedupes_and_defaults(self) -> None:
        self.assertEqual(_normalize_token_scopes(["agent:invoke", " agent:invoke ", ""]), ["agent:invoke"])
        self.assertEqual(_normalize_token_scopes([]), ["agent:invoke"])

    def test_normalize_token_policy_dedupes_and_filters_supported_fields(self) -> None:
        policy = _normalize_token_policy(
            {
                "allowed_agents": ["researcher", " researcher ", "coder"],
                "allowed_memory_modes": ["none", "invalid", "session", "none"],
                "max_trace_level": "debug",
                "custom": {"keep": True},
            }
        )

        self.assertEqual(policy["allowed_agents"], ["researcher", "coder"])
        self.assertEqual(policy["allowed_memory_modes"], ["none", "session"])
        self.assertEqual(policy["max_trace_level"], "debug")
        self.assertEqual(policy["custom"], {"keep": True})

    def test_normalize_token_policy_drops_invalid_trace_level(self) -> None:
        policy = _normalize_token_policy({"max_trace_level": "verbose"})

        self.assertNotIn("max_trace_level", policy)

    def test_generate_and_hash_api_token(self) -> None:
        token, token_prefix = generate_api_token()

        self.assertTrue(token.startswith("cvt_"))
        self.assertIn(f"cvt_{token_prefix}_", token)
        self.assertEqual(hash_api_token(token, "pepper"), hash_api_token(token, "pepper"))
        self.assertNotEqual(hash_api_token(token, "pepper"), hash_api_token(token, "other-pepper"))

    def test_require_scope_rejects_missing_scope(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agents:read"}),
            policy={},
        )

        with self.assertRaises(HTTPException) as context:
            require_scope(principal, "agent:invoke")

        self.assertEqual(context.exception.status_code, 403)

    def test_policy_guards_accept_allowed_values(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={
                "allowed_agents": ["researcher"],
                "allowed_memory_modes": ["none"],
                "max_trace_level": "steps",
            },
        )

        require_scope(principal, "agent:invoke")
        require_agent_allowed(principal, "researcher")
        require_memory_mode_allowed(principal, "none")
        require_trace_level_allowed(principal, "steps")

    def test_policy_guards_reject_disallowed_values(self) -> None:
        principal = ApiPrincipal(
            user_id="user_1",
            workspace_id="workspace_1",
            token_id="token_1",
            token_prefix="prefix",
            scopes=frozenset({"agent:invoke"}),
            policy={
                "allowed_agents": ["researcher"],
                "allowed_memory_modes": ["none"],
                "max_trace_level": "steps",
            },
        )

        for check in [
            lambda: require_agent_allowed(principal, "coder"),
            lambda: require_memory_mode_allowed(principal, "session"),
            lambda: require_trace_level_allowed(principal, "debug"),
        ]:
            with self.assertRaises(HTTPException):
                check()


class PublicInvokeStreamEventTests(unittest.TestCase):
    def test_assistant_event_maps_to_message_delta(self) -> None:
        events = _public_stream_events("assistant", {"text": "hello"}, trace_level="none")

        self.assertEqual(len(events), 1)
        self.assertIn("event: message.delta", events[0])
        self.assertEqual(json.loads(events[0].split("data: ", 1)[1]), {"text": "hello"})

    def test_trace_none_suppresses_tool_events(self) -> None:
        events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="none",
        )

        self.assertEqual(events, [])

    def test_steps_trace_redacts_tool_arguments_and_results(self) -> None:
        call_events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="steps",
        )
        result_events = _public_stream_events(
            "tool_results",
            {"results": [{"tool_call_id": "call_1", "name": "read_file", "content": "secret text"}]},
            trace_level="steps",
        )

        self.assertIn("event: tool.call.started", call_events[0])
        self.assertIn('"arguments": "[redacted]"', call_events[0])
        self.assertIn("event: tool.call.completed", result_events[0])
        self.assertIn('"summary": "[redacted]"', result_events[0])

    def test_debug_trace_includes_tool_arguments_and_summarized_results(self) -> None:
        call_events = _public_stream_events(
            "tool_calls",
            {"tool_calls": [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]},
            trace_level="debug",
        )
        result_events = _public_stream_events(
            "tool_results",
            {"results": [{"tool_call_id": "call_1", "name": "read_file", "content": "visible text"}]},
            trace_level="debug",
        )

        self.assertIn('"path": "README.md"', call_events[0])
        self.assertIn('"summary": "visible text"', result_events[0])


if __name__ == "__main__":
    unittest.main()
