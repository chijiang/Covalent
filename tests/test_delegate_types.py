from __future__ import annotations

import unittest

from covalent.core.types import (
    DelegateRunResult, DelegateRunStatus, Message, ParentInputRequest, RunContext, ToolResult,
)


class DelegateTypeTests(unittest.TestCase):
    def test_delegate_run_result_round_trip_and_defaults(self) -> None:
        result = DelegateRunResult(
            delegate_run_id="delegate-1", agent_name="researcher", status=DelegateRunStatus.IDLE
        )
        dumped = result.model_dump(mode="json")
        self.assertEqual(dumped["status"], "idle")
        self.assertEqual(dumped["error"], {})
        self.assertIsNone(dumped["request"])
        revived = DelegateRunResult.model_validate(dumped)
        self.assertEqual(revived.status, DelegateRunStatus.IDLE)

    def test_parent_input_request_defaults_tool_name(self) -> None:
        request = ParentInputRequest(
            id="question-1", delegate_run_id="delegate-1", title="Need target"
        )
        self.assertEqual(request.tool_name, "ask_parent")
        self.assertEqual(request.questions, [])

    def test_tool_result_carries_parent_request_without_input_request(self) -> None:
        request = ParentInputRequest(
            id="question-1", delegate_run_id="delegate-1", title="Need target"
        )
        result = ToolResult(name="ask_parent", content="Waiting", tool_call_id="c1", parent_request=request)
        self.assertIsNone(result.input_request)
        self.assertEqual(result.parent_request.delegate_run_id, "delegate-1")
        self.assertEqual(result.to_message().tool_calls, [])  # to_message still works

    def test_run_context_memory_scope_defaults(self) -> None:
        context = RunContext(agent_name="a")
        self.assertEqual(context.memory_scope_kind, "session")
        self.assertIsNone(context.memory_scope_id)
        self.assertIsNone(context.delegate_run_id)
        delegated = context.model_copy(update={"memory_scope_kind": "delegate", "memory_scope_id": "delegate-1", "delegate_run_id": "delegate-1", "parent_delegate_run_id": None})
        self.assertEqual(delegated.memory_scope_kind, "delegate")


if __name__ == "__main__":
    unittest.main()
