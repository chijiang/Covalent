"""End-to-end tests for the ReAct agent loop.

Uses ``ScriptedModelAdapter`` to drive the real ``ReactAgentRuntime`` with
canned model responses — no real LLM, no network, no Docker. Validates the
core execution path: tool calling, session persistence, max iterations.
"""

from __future__ import annotations

import asyncio
import unittest

from agent_framework.core.types import GenerationRequest, GenerationResponse, Message, RunContext, TokenUsage, ToolCall
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.skills.meta_tools import (
    READ_SKILL_INSTRUCTIONS_TOOL,
    READ_SKILL_RESOURCE_TOOL,
    register_skill_meta_tools,
)
from agent_framework.skills.spec import ManifestSkillSpec

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

    async def test_skill_prompt_uses_progressive_disclosure(self) -> None:
        """Skill bodies stay out of the system prompt; instructions are readable on demand."""
        full_instructions = "DETAILED_SECRET_WORKFLOW " * 50
        model = ScriptedModelAdapter([text_response("Done.")])
        agent = make_test_agent().model_copy(update={"skills": ["deep-skill"]})
        registry = make_test_registry(agent, model=model)
        registry.register_manifest_skill(
            ManifestSkillSpec(
                name="deep-skill",
                description="Use for deep skill workflows.",
                instructions=full_instructions,
                source_dir="/tmp",
                eager_resource_files=["references/quickstart.md"],
            )
        )
        register_skill_meta_tools(registry)
        runtime = make_test_runtime(registry)

        await runtime.run(agent, "Use the deep skill", RunContext(agent_name="test", session_id="s1"))

        request = model.received_requests[0]
        system_prompt = request.system_prompt or ""
        self.assertIn("deep-skill", system_prompt)
        self.assertIn("Use for deep skill workflows.", system_prompt)
        self.assertIn(READ_SKILL_INSTRUCTIONS_TOOL, system_prompt)
        self.assertIn("references/quickstart.md", system_prompt)
        self.assertNotIn("DETAILED_SECRET_WORKFLOW", system_prompt)
        self.assertNotIn("## Resource:", system_prompt)
        tool_names = {tool["function"]["name"] for tool in request.tools}
        self.assertIn(READ_SKILL_INSTRUCTIONS_TOOL, tool_names)
        self.assertIn(READ_SKILL_RESOURCE_TOOL, tool_names)

    async def test_model_call_trace_includes_raw_request_and_response(self) -> None:
        """Model call trace events include inspectable raw request/response details."""
        response = text_response("Done.").model_copy(
            update={"raw_response": {"id": "resp-1", "choices": [{"message": {"content": "Done."}}]}}
        )
        model = ScriptedModelAdapter([response])
        agent = make_test_agent()
        registry = make_test_registry(agent, model=model)
        runtime = make_test_runtime(registry)

        events = [
            event
            async for event in runtime.stream_events(
                agent,
                "Hello",
                RunContext(agent_name="test", session_id="s1"),
            )
        ]
        model_call = next(event for event in events if event["event"] == "model_call")
        payload = model_call["payload"]

        self.assertEqual(payload["raw_request"]["model"], "test-model")
        self.assertEqual(payload["raw_request"]["messages"][0]["content"], "Hello")
        self.assertEqual(payload["raw_response"]["id"], "resp-1")


class ConcurrentDelegateTests(unittest.IsolatedAsyncioTestCase):
    """Concurrent execution of multiple subagent (delegate) tool calls in one batch.

    The model emits N ``agent__<name>`` tool calls in a single response; the
    runtime must run them concurrently (asyncio.gather), tag every emitted
    ``delegate_*`` event with ``delegate_tool_call_id``, and isolate failures so
    one failing delegate does not block its siblings.
    """

    @staticmethod
    def _multi_delegate_parent_response(call_ids: list[str]) -> "GenerationResponse":
        """A parent model response that calls multiple delegate tools at once."""
        import json as _json

        raw_calls = [
            {
                "id": cid,
                "type": "function",
                "function": {"name": f"agent__sub_{cid}", "arguments": _json.dumps({"input": f"task-{cid}"})},
            }
            for cid in call_ids
        ]
        return GenerationResponse(
            output_text="",
            tool_calls=[
                ToolCall(id=cid, name=f"agent__sub_{cid}", arguments={"input": f"task-{cid}"}, raw=raw)
                for cid, raw in zip(call_ids, raw_calls)
            ],
            assistant_message=Message(role="assistant", content="", tool_calls=raw_calls),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    @staticmethod
    def _build_setup(
        call_ids: list[str],
        *,
        sub_models: dict[str, "ScriptedModelAdapter"],
    ) -> tuple["AgentSpec", "FrameworkRegistry", "ReactAgentRuntime"]:
        parent_model = ScriptedModelAdapter([
            ConcurrentDelegateTests._multi_delegate_parent_response(call_ids),
            text_response("All delegates done."),
        ])
        parent = make_test_agent(name="parent", model="model-p").model_copy(
            update={"delegate_agents": [f"sub_{cid}" for cid in call_ids]}
        )
        registry = make_test_registry(parent, model=parent_model)
        for cid, sub_model in sub_models.items():
            sub_name = f"sub_{cid}"
            sub_agent = make_test_agent(name=sub_name, model=f"model-{cid}", description=f"Delegate {cid}")
            registry.register_agent(sub_agent)
            registry.model_providers[sub_agent.provider.cache_key()] = sub_model
        runtime = make_test_runtime(registry)
        return parent, registry, runtime

    async def test_delegates_run_concurrently(self) -> None:
        """Two delegates that each sleep must interleave: both 'started' before either 'final'."""
        call_ids = ["tc1", "tc2"]

        class _DelayedAdapter(ScriptedModelAdapter):
            def __init__(self, responses, delay: float) -> None:
                super().__init__(responses)
                self._delay = delay

            async def generate(self, request: GenerationRequest) -> GenerationResponse:
                await asyncio.sleep(self._delay)
                return await super().generate(request)

        sub_models = {
            "tc1": _DelayedAdapter([text_response("answer-a")], delay=0.05),
            "tc2": _DelayedAdapter([text_response("answer-b")], delay=0.05),
        }
        parent, _registry, runtime = self._build_setup(call_ids, sub_models=sub_models)

        events = [
            event
            async for event in runtime.stream_events(
                parent, "run both", RunContext(agent_name="parent")
            )
        ]

        started_indices = [i for i, e in enumerate(events) if e["event"] == "delegate_iteration"]
        # delegate_started is emitted as a delegate_thought with kind=delegate_started.
        started_thoughts = [
            i
            for i, e in enumerate(events)
            if e["event"] == "delegate_thought" and e["payload"].get("kind") == "delegate_started"
        ]
        finals = [i for i, e in enumerate(events) if e["event"] == "delegate_final"]

        self.assertEqual(len(started_thoughts), 2, f"expected 2 delegate_started, got {started_thoughts}")
        self.assertGreaterEqual(len(finals), 2, f"expected >=2 delegate_final, got {finals}")
        first_final = finals[0]
        # Under concurrency both delegates start before either finishes.
        for started_idx in started_thoughts:
            self.assertLess(started_idx, first_final,
                            f"delegate_started at {started_idx} must precede first delegate_final at {first_final}")
        # Sanity: there are iteration events too.
        self.assertGreater(len(started_indices), 0)

    async def test_all_delegate_results_present(self) -> None:
        """Both delegate results return to the parent regardless of completion order."""
        call_ids = ["tc1", "tc2"]
        sub_models = {
            "tc1": ScriptedModelAdapter([text_response("answer-a")]),
            "tc2": ScriptedModelAdapter([text_response("answer-b")]),
        }
        parent, _registry, runtime = self._build_setup(call_ids, sub_models=sub_models)

        events = [
            event
            async for event in runtime.stream_events(
                parent, "run both", RunContext(agent_name="parent")
            )
        ]

        # The parent's final answer reflects its post-delegate text turn.
        final_events = [e for e in events if e["event"] == "final"]
        self.assertTrue(final_events, "parent should produce a final response")
        self.assertIn("done", str(final_events[-1]["payload"].get("output_text", "")).lower())

        # tool_results event carries both delegate results.
        tool_results_events = [e for e in events if e["event"] == "tool_results"]
        self.assertTrue(tool_results_events, "expected a tool_results event")
        results = tool_results_events[-1]["payload"]["results"]
        result_by_id = {r.get("tool_call_id"): r for r in results}
        self.assertEqual(set(result_by_id.keys()), set(call_ids),
                         f"expected results for {call_ids}, got {list(result_by_id.keys())}")
        contents = " ".join(str(r.get("content", "")) for r in results)
        self.assertIn("answer-a", contents)
        self.assertIn("answer-b", contents)

    async def test_delegate_failure_does_not_block_sibling(self) -> None:
        """One delegate raising must not prevent the other from completing."""
        call_ids = ["tc1", "tc2"]

        class _FailingAdapter(ScriptedModelAdapter):
            async def generate(self, request: GenerationRequest) -> GenerationResponse:
                raise RuntimeError("subagent tc1 exploded")

        sub_models = {
            "tc1": _FailingAdapter([]),
            "tc2": ScriptedModelAdapter([text_response("answer-b")]),
        }
        parent, _registry, runtime = self._build_setup(call_ids, sub_models=sub_models)

        events = [
            event
            async for event in runtime.stream_events(
                parent, "run both", RunContext(agent_name="parent")
            )
        ]

        # The sibling's final response still arrives.
        delegate_finals = [
            e["payload"] for e in events
            if e["event"] == "delegate_final" and e["payload"].get("delegate_tool_call_id") == "tc2"
        ]
        self.assertTrue(delegate_finals, "sibling delegate tc2 should have completed")

        # The failing delegate emits a delegate_error tagged with its own id.
        errors = [
            e["payload"] for e in events
            if e["event"] == "delegate_error" and e["payload"].get("delegate_tool_call_id") == "tc1"
        ]
        self.assertTrue(errors, "failing delegate tc1 should emit a delegate_error event")

        # The parent still reaches a final response.
        parent_finals = [e for e in events if e["event"] == "final"]
        self.assertTrue(parent_finals, "parent should still produce a final response")

    async def test_every_delegate_event_carries_tool_call_id(self) -> None:
        """All forwarded delegate_* events must carry the correct delegate_tool_call_id."""
        call_ids = ["tc1", "tc2"]
        sub_models = {
            "tc1": ScriptedModelAdapter([text_response("answer-a")]),
            "tc2": ScriptedModelAdapter([text_response("answer-b")]),
        }
        parent, _registry, runtime = self._build_setup(call_ids, sub_models=sub_models)

        events = [
            event
            async for event in runtime.stream_events(
                parent, "run both", RunContext(agent_name="parent")
            )
        ]

        delegate_events = [e for e in events if str(e["event"]).startswith("delegate_")]
        self.assertTrue(delegate_events, "expected delegate_* events")

        # Every delegate event must carry a non-empty id that matches one of the batch ids.
        for event in delegate_events:
            payload = event.get("payload") or {}
            tc_id = payload.get("delegate_tool_call_id")
            self.assertIsInstance(tc_id, str, f"delegate event {event['event']} missing string tool_call_id")
            self.assertIn(tc_id, call_ids, f"unexpected delegate_tool_call_id {tc_id!r}")

        # Each id is represented, and events partition cleanly by id.
        ids_seen = {e["payload"]["delegate_tool_call_id"] for e in delegate_events}
        self.assertEqual(ids_seen, set(call_ids))

    async def test_batch_counts_as_one_parent_iteration(self) -> None:
        """A batch of N delegate calls counts as ONE parent iteration event sequence."""
        call_ids = ["tc1", "tc2"]
        sub_models = {
            "tc1": ScriptedModelAdapter([text_response("answer-a")]),
            "tc2": ScriptedModelAdapter([text_response("answer-b")]),
        }
        parent, _registry, runtime = self._build_setup(call_ids, sub_models=sub_models)

        events = [
            event
            async for event in runtime.stream_events(
                parent, "run both", RunContext(agent_name="parent")
            )
        ]

        # Parent emits exactly one iteration event for the tool-call batch (iteration=1)
        # and possibly one for the final-answer turn (iteration=2). The batch itself is one.
        batch_iterations = [
            e["payload"]["iteration"]
            for e in events
            if e["event"] == "iteration" and e["payload"].get("iteration") == 1
        ]
        self.assertEqual(len(batch_iterations), 1, "the delegate batch should be a single parent iteration")


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
