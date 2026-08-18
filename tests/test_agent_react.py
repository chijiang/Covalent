"""End-to-end tests for the ReAct agent loop.

Uses ``ScriptedModelAdapter`` to drive the real ``ReactAgentRuntime`` with
canned model responses — no real LLM, no network, no Docker. Validates the
core execution path: tool calling, session persistence, max iterations.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from covalent.core.types import GenerationRequest, GenerationResponse, Message, RunContext, TokenUsage, ToolCall
from covalent.infra.delegate_repository import DelegateRunRecord, InMemoryDelegateRunStore
from covalent.infra.memory import InMemorySessionStore
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.memory_port import RuntimeMemoryAdapter
from covalent.runtime.react import ReactAgentRuntime
from covalent.skills.meta_tools import (
    READ_SKILL_INSTRUCTIONS_TOOL,
    READ_SKILL_RESOURCE_TOOL,
    register_skill_meta_tools,
)
from covalent.skills.spec import ManifestSkillSpec

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
        from covalent.core.types import GenerationResponse, Message, TokenUsage, ToolCall
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

    async def test_system_prompt_includes_workspace_confinement_policy(self) -> None:
        """Workspace confinement policy is appended even when the agent has a custom system prompt."""
        model = ScriptedModelAdapter([text_response("Done.")])
        agent = make_test_agent(system_prompt="Custom agent prompt.")
        registry = make_test_registry(agent, model=model)
        runtime = make_test_runtime(registry)

        await runtime.run(agent, "Hi", RunContext(agent_name="test", session_id="s1"))

        system_prompt = model.received_requests[0].system_prompt or ""
        self.assertIn("Custom agent prompt.", system_prompt)
        self.assertIn("inside the session workspace", system_prompt)
        self.assertIn("tmp folder inside the workspace", system_prompt)

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


class DelegateSessionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """M6: a delegate sub-agent must inherit the parent's session_id so its
    trace/final answer is persisted into the same conversation. Before the fix,
    ``_build_delegate_context`` passed session_id=None and the sub-agent's
    history was lost on replay.
    """

    def test_delegate_context_inherits_parent_session_id(self) -> None:
        registry = make_test_registry(make_test_agent(name="parent"))
        runtime = make_test_runtime(registry)
        parent = make_test_agent(name="parent", model="m-p").model_copy(update={"delegate_agents": ["child"]})
        child = make_test_agent(name="child", model="m-c")

        parent_context = RunContext(
            agent_name="parent", session_id="shared-session",
            metadata={"memory_mode": "session"},
        )
        delegate_context = runtime._build_delegate_context(parent, child, parent_context)

        self.assertEqual(delegate_context.session_id, "shared-session",
                         "delegate must inherit the parent session_id so its run persists")
        self.assertEqual(delegate_context.metadata.get("delegated_by"), "parent")
        self.assertEqual(delegate_context.metadata.get("memory_mode"), "session",
                         "delegate must inherit the parent memory_mode")
        self.assertEqual(delegate_context.metadata.get("delegation_chain"), ["parent"])

    def test_delegate_context_no_parent_session(self) -> None:
        """When the parent has no session_id, the delegate also has none — no
        spurious persistence to a non-existent session."""
        registry = make_test_registry(make_test_agent(name="parent"))
        runtime = make_test_runtime(registry)
        parent = make_test_agent(name="parent", model="m-p").model_copy(update={"delegate_agents": ["child"]})
        child = make_test_agent(name="child", model="m-c")

        delegate_context = runtime._build_delegate_context(parent, child, RunContext(agent_name="parent"))
        self.assertIsNone(delegate_context.session_id)


class StatefulDelegateMemoryTests(unittest.IsolatedAsyncioTestCase):
    """Explicit memory scopes: a delegate whose context carries
    memory_scope_kind='delegate' persists its transcript to its own delegate
    run store (root session memory untouched), while legacy flag-off
    delegates (delegated_by metadata only, no memory_scope) keep today's
    behavior exactly — they read the parent conversation but never write
    anywhere.
    """

    _NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _runtime(
        registry: FrameworkRegistry,
        session_store: InMemorySessionStore,
        delegate_store: InMemoryDelegateRunStore,
    ) -> ReactAgentRuntime:
        return ReactAgentRuntime(
            registry,
            session_store=session_store,
            memory_store=RuntimeMemoryAdapter(session_store, delegate_store),
            enable_llm_summarization=False,
        )

    async def test_delegate_with_memory_scope_persists_to_own_store_not_session(self) -> None:
        delegate_store = InMemoryDelegateRunStore()
        await delegate_store.create_run(DelegateRunRecord(
            id="delegate-1",
            session_id="s1",
            execution_scope_id="scope-1",
            workspace_scope_id="ws-1",
            root_agent_name="root",
            parent_agent_name="root",
            delegate_agent_name="child",
            created_at=self._NOW,
            last_activity_at=self._NOW,
        ))
        session_store = InMemorySessionStore()
        child = make_test_agent(name="child", model="m-child")
        model = ScriptedModelAdapter([text_response("Child done.")])
        registry = make_test_registry(child, model=model)
        runtime = self._runtime(registry, session_store, delegate_store)
        delegate_context = RunContext(
            agent_name="child",
            session_id="s1",
            memory_scope_kind="delegate",
            memory_scope_id="delegate-1",
            delegate_run_id="delegate-1",
            metadata={"delegated_by": "parent"},
        )

        response = await runtime.run(child, "task", delegate_context)

        self.assertEqual(response.output_text, "Child done.")
        saved = await delegate_store.load_messages("delegate-1")
        self.assertTrue(any(m.role == "assistant" for m in saved),
                        "delegate transcript should be persisted to its own store")
        self.assertIn("task", [str(m.content) for m in saved if m.role == "user"])
        self.assertEqual(await session_store.load_messages("s1"), [],
                         "the root session memory must stay untouched")

    async def test_flag_off_delegates_still_never_persist(self) -> None:
        delegate_store = InMemoryDelegateRunStore()
        session_store = InMemorySessionStore()
        await session_store.save_messages("s1", [Message(role="user", content="prior context")])
        child = make_test_agent(name="child", model="m-child")
        model = ScriptedModelAdapter([text_response("Legacy answer.")])
        registry = make_test_registry(child, model=model)
        runtime = self._runtime(registry, session_store, delegate_store)
        # Legacy delegate context: delegated_by metadata only, no memory_scope.
        delegate_context = RunContext(
            agent_name="child",
            session_id="s1",
            metadata={"delegated_by": "parent", "memory_mode": "session"},
        )

        response = await runtime.run(child, "legacy task", delegate_context)

        self.assertEqual(response.output_text, "Legacy answer.")
        # Read-only on the parent conversation is preserved: the delegate
        # still SEES prior session memory...
        seen = " ".join(str(m.content) for m in model.received_requests[0].messages)
        self.assertIn("prior context", seen)
        # ...but writes nothing anywhere.
        self.assertEqual([str(m.content) for m in await session_store.load_messages("s1")],
                         ["prior context"],
                         "legacy delegate must not write to the session store")
        self.assertEqual(await delegate_store.count_messages("delegate-1"), 0,
                         "legacy delegate must not write to the delegate store")


    async def test_explicit_delegate_scope_beats_memory_mode_none(self) -> None:
        """Scope resolution order: an explicit delegate scope wins over
        metadata memory_mode=none. A delegate spawned from a stateless parent
        (memory_mode=none inherited) that is granted its own delegate scope
        must still persist to that scope — swapping _memory_scope's first two
        branches would silently turn every stateful delegate memoryless."""
        delegate_store = InMemoryDelegateRunStore()
        session_store = InMemorySessionStore()
        child = make_test_agent(name="child", model="m-child")
        model = ScriptedModelAdapter([text_response("Scoped answer.")])
        registry = make_test_registry(child, model=model)
        runtime = self._runtime(registry, session_store, delegate_store)
        delegate_context = RunContext(
            agent_name="child",
            session_id="s1",
            memory_scope_kind="delegate",
            memory_scope_id="delegate-2",
            delegate_run_id="delegate-2",
            metadata={"delegated_by": "parent", "memory_mode": "none"},
        )

        response = await runtime.run(child, "scoped task", delegate_context)

        self.assertEqual(response.output_text, "Scoped answer.")
        saved = await delegate_store.load_messages("delegate-2")
        self.assertTrue(any(m.role == "assistant" for m in saved),
                        "explicit delegate scope must persist even with memory_mode=none")
        self.assertEqual(await session_store.load_messages("s1"), [],
                         "nothing may land in the session store")


if __name__ == "__main__":
    unittest.main()
