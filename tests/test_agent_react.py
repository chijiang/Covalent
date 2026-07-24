"""End-to-end tests for the ReAct agent loop.

Uses ``ScriptedModelAdapter`` to drive the real ``ReactAgentRuntime`` with
canned model responses — no real LLM, no network, no Docker. Validates the
core execution path: tool calling, session persistence, max iterations.
"""

from __future__ import annotations

import unittest

from agent_framework.core.types import RunContext

from tests.helpers import (
    ScriptedModelAdapter,
    make_test_agent,
    make_test_registry,
    make_test_runtime,
    text_response,
    tool_call_response,
)

_ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo_tool",
        "description": "Echoes back the message.",
        "parameters": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    },
}


def _echo_handler(args, ctx):
    import json
    return json.dumps({"echo": args.get("msg", "")})


class ReactLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_react_no_tools_single_turn(self) -> None:
        """Model returns text immediately — no tool calls, one model call."""
        model = ScriptedModelAdapter([text_response("Hello!")])
        agent = make_test_agent(local_tools=["echo_tool"])
        registry = make_test_registry(agent, model=model, tools={"echo_tool": (_ECHO_SCHEMA, _echo_handler)})
        runtime = make_test_runtime(registry)

        response = await runtime.run(agent, "Hi", RunContext(agent_name="test", session_id="s1"))
        self.assertEqual(response.output_text, "Hello!")
        self.assertEqual(model.call_count, 1)

    async def test_react_calls_tool_then_answers(self) -> None:
        """Model calls echo_tool → result returned → model gives final answer."""
        model = ScriptedModelAdapter([
            tool_call_response("echo_tool", arguments={"msg": "world"}, call_id="c1"),
            text_response("Echo: world"),
        ])
        agent = make_test_agent(local_tools=["echo_tool"])
        registry = make_test_registry(agent, model=model, tools={"echo_tool": (_ECHO_SCHEMA, _echo_handler)})
        runtime = make_test_runtime(registry)

        response = await runtime.run(agent, "Echo world", RunContext(agent_name="test", session_id="s1"))
        self.assertIn("Echo", response.output_text)
        self.assertEqual(model.call_count, 2)  # tool-call turn + final turn

    async def test_react_max_iterations_exhausted(self) -> None:
        """Model always calls tools → loop exhausts → forced text-only response."""
        model = ScriptedModelAdapter([
            tool_call_response("echo_tool", arguments={"msg": f"iter-{i}"}, call_id=f"c{i}")
            for i in range(10)
        ] + [text_response("I give up.")])
        agent = make_test_agent(max_iterations=3, local_tools=["echo_tool"])
        registry = make_test_registry(agent, model=model, tools={"echo_tool": (_ECHO_SCHEMA, _echo_handler)})
        runtime = make_test_runtime(registry)

        response = await runtime.run(agent, "Loop", RunContext(agent_name="test", session_id="s1"))
        # The forced final call happens after max_iterations+2 model calls.
        self.assertLessEqual(model.call_count, agent.max_iterations + 3)
        self.assertIsInstance(response.output_text, str)

    async def test_react_multiple_tool_calls_in_one_turn(self) -> None:
        """Model calls two tools in one response → both executed → model answers."""
        from agent_framework.core.types import GenerationResponse, Message, TokenUsage, ToolCall
        import json

        raw_calls = [
            {"id": "c1", "type": "function", "function": {"name": "echo_tool", "arguments": json.dumps({"msg": "first"})}},
            {"id": "c2", "type": "function", "function": {"name": "echo_tool", "arguments": json.dumps({"msg": "second"})}},
        ]
        multi_tool = GenerationResponse(
            output_text="",
            tool_calls=[
                ToolCall(id="c1", name="echo_tool", arguments={"msg": "first"}, raw=raw_calls[0]),
                ToolCall(id="c2", name="echo_tool", arguments={"msg": "second"}, raw=raw_calls[1]),
            ],
            assistant_message=Message(role="assistant", content="", tool_calls=raw_calls),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )
        model = ScriptedModelAdapter([multi_tool, text_response("Both done.")])
        agent = make_test_agent(local_tools=["echo_tool"])
        registry = make_test_registry(agent, model=model, tools={"echo_tool": (_ECHO_SCHEMA, _echo_handler)})
        runtime = make_test_runtime(registry)

        response = await runtime.run(agent, "Echo both", RunContext(agent_name="test", session_id="s1"))
        self.assertIn("done", response.output_text.lower())
        self.assertEqual(model.call_count, 2)  # tool-call turn + final turn


class SessionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_persistence_across_runs(self) -> None:
        """Run 1 saves messages → run 2 with same session_id loads them."""
        model = ScriptedModelAdapter([
            text_response("First answer."),
            text_response("Second answer."),
        ])
        agent = make_test_agent()
        registry = make_test_registry(agent, model=model)
        runtime = make_test_runtime(registry)
        ctx = RunContext(agent_name="test", session_id="s1")

        await runtime.run(agent, "Hello first", ctx)
        await runtime.run(agent, "Hello second", ctx)

        # The first model request has just the user input (system prompt is separate).
        self.assertGreaterEqual(len(model.received_requests[0].messages), 1)
        # The second model request must contain prior history (run-1 messages).
        second_messages = model.received_requests[1].messages
        self.assertGreater(len(second_messages), len(model.received_requests[0].messages),
                           "Second run should have more messages (prior history loaded)")
        # Verify prior user + assistant content is present.
        all_content = " ".join(str(m.content) for m in second_messages if m.content)
        self.assertIn("first", all_content.lower())
        self.assertIn("first answer", all_content.lower())

    async def test_session_not_loaded_with_memory_none(self) -> None:
        """memory_mode=none → run 2 does NOT load run-1 messages."""
        model = ScriptedModelAdapter([
            text_response("First answer."),
            text_response("Second answer."),
        ])
        agent = make_test_agent()
        registry = make_test_registry(agent, model=model)
        runtime = make_test_runtime(registry)

        # Run 1: session mode (persists).
        await runtime.run(agent, "Hello first", RunContext(agent_name="test", session_id="s2"))
        # Run 2: memory_mode=none (does not load prior history).
        await runtime.run(agent, "Hello second",
                          RunContext(agent_name="test", session_id="s2", metadata={"memory_mode": "none"}))

        # The second request should NOT contain run-1 messages.
        second_messages = model.received_requests[1].messages
        all_content = " ".join(str(m.content) for m in second_messages if m.content)
        self.assertNotIn("first answer", all_content.lower(),
                         "Prior messages should not be loaded with memory_mode=none")


if __name__ == "__main__":
    unittest.main()
