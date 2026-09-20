"""End-to-end tests for the ReAct agent loop.

Uses ``ScriptedModelAdapter`` to drive the real ``ReactAgentRuntime`` with
canned model responses — no real LLM, no network, no Docker. Validates the
core execution path: tool calling, session persistence, max iterations.
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from covalent.application.services.delegate_service import (
    DelegateService,
    register_ask_parent_tool,
    register_delegate_lifecycle_tools,
)
from covalent.core.agent import AgentSpec
from covalent.core.types import (
    DelegateRunResult,
    DelegateRunStatus,
    GenerationRequest,
    GenerationResponse,
    Message,
    ParentInputRequest,
    RunContext,
    TokenUsage,
    ToolCall,
    UserInputRequest,
    UserQuestion,
)
from covalent.infra.delegate_repository import DelegateRunRecord, InMemoryDelegateRunStore
from covalent.infra.memory import InMemorySessionStore
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.delegation import DelegateActor, DelegateTurnOutcome
from covalent.runtime.memory_port import RuntimeMemoryAdapter
from covalent.runtime import react as react_module
from covalent.runtime.react import ReactAgentRuntime
from covalent.skills.meta_tools import (
    READ_SKILL_INSTRUCTIONS_TOOL,
    READ_SKILL_RESOURCE_TOOL,
    register_skill_meta_tools,
)
from covalent.skills.spec import ManifestSkillSpec

from tests.helpers import (
    _EnvelopeRunIdAdapter,
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


#: Delegate lifecycle SSE event names (mirrors covalent.api.sse_events).
DELEGATE_LIFECYCLE_EVENT_NAMES = (
    "delegate_created",
    "delegate_running",
    "delegate_waiting_parent",
    "delegate_resumed",
    "delegate_idle",
    "delegate_released",
    "delegate_cancelled",
    "delegate_expired",
    "delegate_failed",
)


def _lifecycle_sequence(events: list[dict]) -> list[str]:
    """Ordered lifecycle event names from a stream, for boundary assertions."""
    return [e["event"] for e in events if e["event"] in DELEGATE_LIFECYCLE_EVENT_NAMES]


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

        # Legacy path (no coordinator) never emits lifecycle events.
        self.assertEqual(_lifecycle_sequence(events), [])

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


_ASK_USER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "Ask the end user a question.",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["questions"],
        },
    },
}


def _fake_ask_user_handler(_args, _ctx):
    return UserInputRequest(
        id="qu-1",
        tool_name="ask_user",
        title="Need input",
        questions=[UserQuestion(header="H", question="Q")],
    )


class StatefulDelegateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Stateful delegate path: the runtime drives DelegateService-backed runs.

    Delegate tool calls return JSON envelopes, children see ask_parent instead
    of ask_user, delegate memory is isolated per run, and a waiting_parent
    pause surfaces to the parent as an ordinary envelope tool result — the
    parent answers with delegate_send or escalates on its own, and a waiting
    child never becomes a top-level input_required.
    """

    @staticmethod
    def _build(
        parent_responses: list[GenerationResponse],
        child_responses: list[GenerationResponse],
        *,
        child_local_tools: list[str] | None = None,
        parent_local_tools: list[str] | None = None,
        register_ask_user: bool = False,
        child_model: ScriptedModelAdapter | None = None,
        settings: AppSettings | None = None,
        child_delegate_agents: list[str] | None = None,
        extra_agents: dict[str, ScriptedModelAdapter] | None = None,
        parent_model: ScriptedModelAdapter | None = None,
    ) -> SimpleNamespace:
        session_store = InMemorySessionStore()
        run_store = InMemoryDelegateRunStore()
        parent = make_test_agent(
            name="parent", model="m-parent", local_tools=parent_local_tools
        ).model_copy(update={"delegate_agents": ["child"]})
        child = make_test_agent(name="child", model="m-child", local_tools=child_local_tools)
        if child_delegate_agents:
            child = child.model_copy(update={"delegate_agents": child_delegate_agents})
        parent_adapter = parent_model or ScriptedModelAdapter(parent_responses)
        child_adapter = child_model or ScriptedModelAdapter(child_responses)
        registry = make_test_registry(parent, model=parent_adapter)
        registry.register_agent(child)
        registry.model_providers[child.provider.cache_key()] = child_adapter
        for agent_name, adapter in (extra_agents or {}).items():
            extra = make_test_agent(name=agent_name, model=f"m-{agent_name}")
            registry.register_agent(extra)
            registry.model_providers[extra.provider.cache_key()] = adapter
        register_ask_parent_tool(registry)
        if register_ask_user:
            registry.register_local_tool(
                "ask_user", _ASK_USER_SCHEMA, handler=_fake_ask_user_handler
            )
        service = DelegateService(
            registry=registry, run_store=run_store, settings=settings or AppSettings()
        )
        register_delegate_lifecycle_tools(registry, service)
        runtime = ReactAgentRuntime(
            registry,
            session_store=session_store,
            memory_store=RuntimeMemoryAdapter(session_store, run_store),
            delegate_coordinator=service,
            enable_llm_summarization=False,
        )
        return SimpleNamespace(
            parent=parent,
            child=child,
            runtime=runtime,
            service=service,
            registry=registry,
            session_store=session_store,
            run_store=run_store,
            parent_model=parent_adapter,
            child_model=child_adapter,
        )

    @staticmethod
    async def _collect(runtime, agent, user_input: str, context: RunContext) -> list[dict]:
        return [event async for event in runtime.stream_events(agent, user_input, context)]

    @staticmethod
    def _tool_result_content(events: list[dict], tool_call_id: str) -> str:
        for event in reversed(events):
            if event["event"] != "tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == tool_call_id:
                    return str(result["content"])
        raise AssertionError(f"no tool result for {tool_call_id!r} in stream")

    @staticmethod
    def _tool_result(events: list[dict], tool_call_id: str) -> dict:
        for event in reversed(events):
            if event["event"] != "tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == tool_call_id:
                    return result
        raise AssertionError(f"no tool result for {tool_call_id!r} in stream")

    async def test_final_answer_returns_idle_envelope_and_persists_private_memory(self) -> None:
        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "Summarize the report"}, call_id="pc-1"),
                text_response("Parent done"),
            ],
            child_responses=[text_response("Child answer")],
        )
        context = RunContext(agent_name="parent", session_id="s1")

        events = await self._collect(fixture.runtime, fixture.parent, "Please delegate", context)

        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)
        self.assertEqual(envelope.output, "Child answer")
        run_id = envelope.delegate_run_id

        # Delegate trace events carry the run identity.
        delegate_finals = [e["payload"] for e in events if e["event"] == "delegate_final"]
        self.assertTrue(delegate_finals, "expected a forwarded delegate_final event")
        self.assertEqual(delegate_finals[-1].get("delegate_run_id"), run_id)

        # Lifecycle events mark the run boundary in order, each with the run
        # identity and a status; the deprecated delegate_input_required never
        # fires on the stateful path.
        self.assertEqual(
            _lifecycle_sequence(events),
            ["delegate_created", "delegate_running", "delegate_idle"],
        )
        for name in ("delegate_created", "delegate_running", "delegate_idle"):
            payload = next(e["payload"] for e in events if e["event"] == name)
            self.assertEqual(payload["delegate_run_id"], run_id)
            self.assertIsInstance(payload["status"], str)
            self.assertIsInstance(payload["summary"], str)
        self.assertFalse([e for e in events if e["event"] == "delegate_input_required"])

        # Delegate memory is private and ends with the child's assistant answer.
        delegate_messages = await fixture.run_store.load_messages(run_id)
        self.assertEqual(delegate_messages[-1].role, "assistant")
        self.assertIn("Child answer", str(delegate_messages[-1].content))

        # Session memory holds only the parent transcript — no capsule, no child turn.
        session_messages = await fixture.session_store.load_messages("s1")
        self.assertNotIn(
            "[delegation]", " ".join(str(m.content) for m in session_messages),
            "delegate capsule must not leak into session memory",
        )
        self.assertEqual(
            [m.role for m in session_messages], ["user", "assistant", "tool", "assistant"],
            "session memory should be exactly the parent transcript",
        )

        # The run row reached IDLE.
        row = await fixture.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.IDLE)

    async def test_ask_parent_pauses_child_and_returns_waiting_envelope(self) -> None:
        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "Deploy the service"}, call_id="pc-1"),
                text_response("The deployer needs a target; I will report back."),
            ],
            child_responses=[
                tool_call_response(
                    "ask_parent",
                    arguments={
                        "title": "Need target",
                        "questions": [{"header": "Target", "question": "Which target?"}],
                    },
                    call_id="ask-1",
                ),
                text_response("Resolved with Docker"),
            ],
        )
        # execution_scope_id matters for the resume leg: delegate ownership is
        # checked against the actor's (agent, run, scope) tuple.
        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )

        events = await self._collect(fixture.runtime, fixture.parent, "Deploy it", parent_context)

        # The child's pause never surfaces as a stream boundary at the root:
        # the waiting envelope is an ordinary tool result, the parent keeps its
        # ReAct loop, and no input_required of any kind fires.
        self.assertFalse(
            [e for e in events if e["event"] in ("input_required", "delegate_input_required", "parent_input_required")]
        )
        self.assertTrue([e for e in events if e["event"] == "final"])

        # The parent's second model call saw the waiting envelope as its
        # agent__child tool result and answered on its own.
        second_request_messages = fixture.parent_model.received_requests[1].messages
        envelope_text = " ".join(str(m.content) for m in second_request_messages if m.role == "tool")
        self.assertIn("waiting_parent", envelope_text)

        # The persisted parent transcript carries the waiting envelope through
        # the assistant agent__child tool call.
        session_messages = await fixture.session_store.load_messages("s1")
        envelope_message = next(
            m for m in session_messages if m.role == "tool" and m.tool_call_id == "pc-1"
        )
        envelope = DelegateRunResult.model_validate(json.loads(str(envelope_message.content)))
        self.assertEqual(envelope.status, DelegateRunStatus.WAITING_PARENT)
        self.assertIsNotNone(envelope.request)
        self.assertEqual(envelope.request.title, "Need target")
        self.assertEqual(envelope.request.tool_call_id, "ask-1")

        # Run row is WAITING_PARENT with the pending request stored.
        run_id = envelope.delegate_run_id
        row = await fixture.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.WAITING_PARENT)
        self.assertIsNotNone(row.pending_request)

        # The pause surfaced as a waiting_parent lifecycle boundary: status,
        # run identity, and the request title in the summary.
        self.assertEqual(
            _lifecycle_sequence(events),
            ["delegate_created", "delegate_running", "delegate_waiting_parent"],
        )
        waiting_payload = next(
            e["payload"] for e in events if e["event"] == "delegate_waiting_parent"
        )
        self.assertEqual(waiting_payload["status"], "waiting_parent")
        self.assertEqual(waiting_payload["delegate_run_id"], run_id)
        self.assertIn("Need target", waiting_payload["summary"])

        # Resuming the run replays the parent's answer and completes the turn.
        handle = await fixture.service.send(
            actor=DelegateActor.from_context(parent_context),
            delegate_run_id=run_id,
            input_text="Use Docker",
        )
        outcome = await fixture.runtime._execute_delegate_turn(
            handle,
            parent_agent=fixture.parent,
            tool_call=ToolCall(id="pc-1", name="agent__child", arguments={"input": "Deploy the service"}),
            parent_context=parent_context,
            parent_iteration=1,
            event_sink=None,
        )
        self.assertEqual(outcome.status, "idle")
        self.assertEqual(outcome.output, "Resolved with Docker")
        result = await fixture.service.report_outcome(handle.run.id, outcome)
        self.assertEqual(result.status, DelegateRunStatus.IDLE)

        resumed_messages = await fixture.run_store.load_messages(run_id)
        self.assertEqual(resumed_messages[-1].role, "assistant")
        self.assertIn("Resolved with Docker", str(resumed_messages[-1].content))

    async def test_ask_user_absent_and_rejected_in_delegated_context(self) -> None:
        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "Do it"}, call_id="pc-1"),
                text_response("Parent done"),
            ],
            child_responses=[
                tool_call_response(
                    "ask_user",
                    arguments={"questions": [{"header": "H", "question": "Q"}]},
                    call_id="au-1",
                ),
                text_response("Continued without user input"),
            ],
            child_local_tools=["ask_user"],
            register_ask_user=True,
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Run the child", RunContext(agent_name="parent", session_id="s1")
        )

        # The child's resolved tool schemas exclude ask_user and include ask_parent.
        child_tool_names = {
            t["function"]["name"] for t in fixture.child_model.received_requests[0].tools
        }
        self.assertNotIn("ask_user", child_tool_names)
        self.assertIn("ask_parent", child_tool_names)

        # Calling ask_user by name yields the execution-side allowlist error:
        # ask_user is not exposed in delegated runs, so the call is rejected
        # before any handler runs.
        converted = None
        for event in events:
            if event["event"] != "delegate_tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == "au-1":
                    converted = result
        self.assertIsNotNone(converted, "expected the converted ask_user result in delegate traces")
        self.assertTrue(converted["is_error"])
        self.assertIn("not available", str(converted["content"]))

        # No input_required of any kind reached the stream; the run completed.
        self.assertFalse(
            [e for e in events if e["event"] in ("input_required", "delegate_input_required")]
        )
        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)

    async def test_ask_parent_absent_in_root_context(self) -> None:
        fixture = self._build(
            parent_responses=[text_response("Direct answer")],
            child_responses=[text_response("child")],
            parent_local_tools=["ask_parent"],
        )

        await fixture.runtime.run(
            fixture.parent, "Hi", RunContext(agent_name="parent", session_id="s1")
        )

        tool_names = {
            t["function"]["name"] for t in fixture.parent_model.received_requests[0].tools
        }
        self.assertNotIn("ask_parent", tool_names, "ask_parent must be filtered out of root tools")
        self.assertIn("agent__child", tool_names, "delegate tools stay available at root")

    async def test_two_runs_same_definition_isolated_memory(self) -> None:
        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "first task"}, call_id="pc-1"),
                tool_call_response("agent__child", arguments={"input": "second task"}, call_id="pc-2"),
                text_response("Both done"),
            ],
            child_responses=[text_response("First answer"), text_response("Second answer")],
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Two tasks", RunContext(agent_name="parent", session_id="s1")
        )

        envelope_1 = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        envelope_2 = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-2"))
        )
        self.assertNotEqual(envelope_1.delegate_run_id, envelope_2.delegate_run_id)
        self.assertEqual(envelope_1.output, "First answer")
        self.assertEqual(envelope_2.output, "Second answer")

        memory_1 = await fixture.run_store.load_messages(envelope_1.delegate_run_id)
        memory_2 = await fixture.run_store.load_messages(envelope_2.delegate_run_id)
        self.assertIn("first task", " ".join(str(m.content) for m in memory_1))
        self.assertNotIn("second task", " ".join(str(m.content) for m in memory_1))
        self.assertIn("second task", " ".join(str(m.content) for m in memory_2))
        self.assertNotIn("first task", " ".join(str(m.content) for m in memory_2))

        for envelope in (envelope_1, envelope_2):
            row = await fixture.run_store.get_run(envelope.delegate_run_id)
            self.assertEqual(row.status, DelegateRunStatus.IDLE)


    async def test_child_failure_reports_failed_envelope(self) -> None:
        class _ExplodingAdapter(ScriptedModelAdapter):
            async def generate(self, request: GenerationRequest) -> GenerationResponse:
                raise RuntimeError("child model exploded")

        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "Do it"}, call_id="pc-1"),
                text_response("Parent saw the failure"),
            ],
            child_responses=[],
            child_model=_ExplodingAdapter([]),
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Run the child", RunContext(agent_name="parent", session_id="s1")
        )

        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.FAILED)
        self.assertEqual(envelope.error.get("code"), "execution_error")
        row = await fixture.run_store.get_run(envelope.delegate_run_id)
        self.assertEqual(row.status, DelegateRunStatus.FAILED)

        # The failure surfaced as a delegate_error trace and the parent still
        # reached its own final answer.
        self.assertTrue([e for e in events if e["event"] == "delegate_error"])
        self.assertTrue([e for e in events if e["event"] == "final"])

    async def test_depth_cap_blocks_nested_delegation(self) -> None:
        session_store = InMemorySessionStore()
        run_store = InMemoryDelegateRunStore()
        parent = make_test_agent(name="parent", model="m-parent").model_copy(
            update={"delegate_agents": ["child"]}
        )
        child = make_test_agent(name="child", model="m-child").model_copy(
            update={"delegate_agents": ["grand"]}
        )
        grand = make_test_agent(name="grand", model="m-grand")
        parent_model = ScriptedModelAdapter([
            tool_call_response("agent__child", arguments={"input": "delegate down"}, call_id="pc-1"),
            text_response("Parent done"),
        ])
        child_model = ScriptedModelAdapter([
            tool_call_response("agent__grand", arguments={"input": "go deeper"}, call_id="cc-1"),
            text_response("Child done"),
        ])
        grand_model = ScriptedModelAdapter([])
        registry = make_test_registry(parent, model=parent_model)
        registry.register_agent(child)
        registry.model_providers[child.provider.cache_key()] = child_model
        registry.register_agent(grand)
        registry.model_providers[grand.provider.cache_key()] = grand_model
        register_ask_parent_tool(registry)
        service = DelegateService(
            registry=registry, run_store=run_store, settings=AppSettings(delegate_max_depth=1)
        )
        runtime = ReactAgentRuntime(
            registry,
            session_store=session_store,
            memory_store=RuntimeMemoryAdapter(session_store, run_store),
            delegate_coordinator=service,
            enable_llm_summarization=False,
        )

        events = await self._collect(
            runtime, parent, "Go", RunContext(agent_name="parent", session_id="s1")
        )

        # The depth cap rejected the grandchild call inside the child's turn
        # with an error tool result; the grandchild never ran.
        grand_results = []
        for event in events:
            if event["event"] != "delegate_tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == "cc-1":
                    grand_results.append(result)
        self.assertTrue(grand_results, "expected the blocked agent__grand result in traces")
        self.assertTrue(grand_results[0]["is_error"])
        self.assertIn("Maximum delegation depth exceeded", str(grand_results[0]["content"]))
        self.assertEqual(grand_model.call_count, 0)

        # The child itself completed normally.
        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)
        self.assertEqual(envelope.output, "Child done")

    async def test_fresh_start_task_appears_once_in_child_memory(self) -> None:
        """The service persists capsule+task into the run's memory before the
        first turn, so the runtime must stream the fresh leg with a blank
        user_input — the child's first model call sees the task exactly once."""
        fixture = self._build(
            parent_responses=[
                tool_call_response(
                    "agent__child", arguments={"input": "Summarize the quarterly report"}, call_id="pc-1"
                ),
                text_response("Parent done"),
            ],
            child_responses=[text_response("Child answer")],
        )

        await self._collect(
            fixture.runtime, fixture.parent, "Please delegate",
            RunContext(agent_name="parent", session_id="s1"),
        )

        first_messages = fixture.child_model.received_requests[0].messages
        user_messages = [m for m in first_messages if m.role == "user"]
        self.assertEqual(
            len(user_messages), 1,
            f"task must reach the child exactly once, saw {[str(m.content)[:60] for m in user_messages]}",
        )
        self.assertEqual(first_messages[-1].role, "user")
        self.assertIn("Summarize the quarterly report", str(first_messages[-1].content))
        self.assertIn("[delegation]", str(first_messages[-1].content))

    async def test_delegate_send_answers_waiting_child_and_child_continues(self) -> None:
        fixture = self._build(
            parent_responses=[],
            child_responses=[
                tool_call_response(
                    "ask_parent",
                    arguments={
                        "title": "Need target",
                        "questions": [{"header": "Target", "question": "Which target?"}],
                    },
                    call_id="ask-1",
                ),
                text_response("Resolved with Docker"),
            ],
            parent_model=_EnvelopeRunIdAdapter([
                tool_call_response(
                    "agent__child", arguments={"input": "Deploy the service"}, call_id="pc-1"
                ),
                tool_call_response(
                    "delegate_send",
                    arguments={"delegate_run_id": "", "input": "Use Docker"},
                    call_id="ps-1",
                ),
                text_response("Parent done"),
            ]),
        )
        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )

        # One parent stream: the child pauses on ask_parent, the parent sees
        # the waiting envelope as its tool result, and answers it with
        # delegate_send in its next model response — no user input involved.
        events = await self._collect(fixture.runtime, fixture.parent, "Deploy it", parent_context)
        self.assertFalse(
            [e for e in events if e["event"] in ("input_required", "delegate_input_required", "parent_input_required")]
        )

        # The lifecycle tools are exposed to the root agent.
        exposed = {
            t["function"]["name"] for t in fixture.parent_model.received_requests[1].tools
        }
        self.assertIn("delegate_send", exposed)
        self.assertIn("delegate_list", exposed)
        self.assertIn("delegate_release", exposed)

        # The child's second generation request resumes right after its saved
        # ask_parent tool call, answered with tool_call_id "ask-1".
        second_messages = fixture.child_model.received_requests[-1].messages
        last = second_messages[-1]
        self.assertEqual(last.role, "tool")
        self.assertEqual(last.tool_call_id, "ask-1")
        self.assertIn("Use Docker", str(last.content))
        ask_assistant = next(
            m for m in second_messages
            if m.role == "assistant" and any(
                isinstance(c, dict) and (
                    c.get("id") == "ask-1"
                    or (c.get("function") or {}).get("name") == "ask_parent"
                )
                for c in m.tool_calls
            )
        )
        self.assertLess(
            second_messages.index(ask_assistant), len(second_messages) - 1,
            "the ask_parent assistant tool_call must precede its tool answer",
        )

        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "ps-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)
        self.assertEqual(envelope.output, "Resolved with Docker")
        row = await fixture.run_store.get_run(envelope.delegate_run_id)
        self.assertEqual(row.status, DelegateRunStatus.IDLE)

        # The send turn's child events flowed through the parent stream.
        send_finals = [
            e["payload"] for e in events
            if e["event"] == "delegate_final"
            and e["payload"].get("delegate_tool_call_id") == "ps-1"
        ]
        self.assertTrue(send_finals, "the resumed child turn must forward its final event")

        # Lifecycle boundaries: the fresh leg pauses, the send leg resumes and
        # idles — resumed/running/idle all tagged to the delegate_send call.
        self.assertEqual(
            _lifecycle_sequence(events),
            [
                "delegate_created",
                "delegate_running",
                "delegate_waiting_parent",
                "delegate_resumed",
                "delegate_running",
                "delegate_idle",
            ],
        )
        resumed_payload = next(e["payload"] for e in events if e["event"] == "delegate_resumed")
        self.assertEqual(resumed_payload["delegate_run_id"], envelope.delegate_run_id)
        self.assertEqual(resumed_payload["delegate_tool_call_id"], "ps-1")
        idle_payload = next(
            e["payload"] for e in events
            if e["event"] == "delegate_idle"
            and e["payload"].get("delegate_tool_call_id") == "ps-1"
        )
        self.assertEqual(idle_payload["status"], "idle")

    async def test_nested_delegate_gets_lifecycle_tools_and_answers_grandchild(self) -> None:
        """A delegate that itself owns delegates receives the lifecycle tools:
        the MIDDLE run answers a grandchild's ask_parent with delegate_send
        inside its own turn — the grandchild resumes and completes without the
        root ever intervening (one level deeper than the root-answers test)."""
        grand_model = ScriptedModelAdapter([
            tool_call_response(
                "ask_parent",
                arguments={
                    "title": "Need target",
                    "questions": [{"header": "Target", "question": "Which target?"}],
                },
                call_id="ask-1",
            ),
            text_response("Grand done"),
        ])
        fixture = self._build(
            parent_responses=[],
            child_responses=[],
            child_model=_EnvelopeRunIdAdapter([
                tool_call_response("agent__grand", arguments={"input": "go deeper"}, call_id="cc-1"),
                tool_call_response(
                    "delegate_send",
                    arguments={"delegate_run_id": "", "input": "Use Docker"},
                    call_id="cs-1",
                ),
                text_response("Child done"),
            ]),
            child_delegate_agents=["grand"],
            extra_agents={"grand": grand_model},
            parent_model=ScriptedModelAdapter([
                tool_call_response("agent__child", arguments={"input": "Do the nested job"}, call_id="pc-1"),
                text_response("Parent done"),
            ]),
        )
        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )

        events = await self._collect(fixture.runtime, fixture.parent, "Go nested", parent_context)

        # The middle delegate's tool schema carries both delegation surfaces:
        # ask_parent (it is delegated) and the lifecycle tools (it owns the
        # grandchild run and must be able to answer it with delegate_send).
        child_tool_names = {
            t["function"]["name"] for t in fixture.child_model.received_requests[1].tools
        }
        self.assertIn("delegate_send", child_tool_names)
        self.assertIn("delegate_list", child_tool_names)
        self.assertIn("delegate_release", child_tool_names)
        self.assertIn("ask_parent", child_tool_names)
        self.assertNotIn("ask_user", child_tool_names)

        # The grandchild paused on ask_parent and its DIRECT parent answered:
        # the resumed grandchild request ends with the ask-1 tool answer.
        grand_second_messages = grand_model.received_requests[-1].messages
        self.assertEqual(grand_second_messages[-1].role, "tool")
        self.assertEqual(grand_second_messages[-1].tool_call_id, "ask-1")
        self.assertIn("Use Docker", str(grand_second_messages[-1].content))

        # The send leg's envelope surfaced inside the child's turn traces and
        # completed idle; the child and grandchild rows both ended idle.
        send_results = []
        for event in events:
            if event["event"] != "delegate_tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == "cs-1":
                    send_results.append(result)
        self.assertTrue(send_results, "the middle delegate's send result must appear in traces")
        grand_envelope = DelegateRunResult.model_validate(
            json.loads(str(send_results[0]["content"]))
        )
        self.assertEqual(grand_envelope.status, DelegateRunStatus.IDLE)
        self.assertEqual(grand_envelope.output, "Grand done")
        grand_row = await fixture.run_store.get_run(grand_envelope.delegate_run_id)
        self.assertEqual(grand_row.status, DelegateRunStatus.IDLE)
        self.assertEqual(grand_row.parent_agent_name, "child")

        child_envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(child_envelope.status, DelegateRunStatus.IDLE)
        self.assertEqual(child_envelope.output, "Child done")

    async def test_delegate_release_survives_deleted_delegate_agent(self) -> None:
        """Release on a terminal run is legal even after the delegate agent
        was deleted from the registry: the released envelope comes back as the
        tool result and the delegate_released event still fires — never a
        crash of the parent stream from the missing agent lookup."""
        fixture = self._build(
            parent_responses=[],
            child_responses=[text_response("Child answer")],
            parent_model=_EnvelopeRunIdAdapter([
                tool_call_response("agent__child", arguments={"input": "One job"}, call_id="pc-1"),
                text_response("Started"),
            ]),
        )
        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )
        first = await self._collect(fixture.runtime, fixture.parent, "Run it", parent_context)
        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(first, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)

        # The delegate agent is removed from the registry after the run went
        # terminal (agent deletion is only blocked while runs are ACTIVE).
        del fixture.registry.agents["child"]
        release_model = _EnvelopeRunIdAdapter([
            tool_call_response(
                "delegate_release",
                arguments={"delegate_run_id": "", "reason": "finished"},
                call_id="pr-1",
            ),
            text_response("Cleaned up"),
        ])
        fixture.registry.model_providers[fixture.parent.provider.cache_key()] = release_model

        second = await self._collect(fixture.runtime, fixture.parent, "Clean up", parent_context)

        released = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(second, "pr-1"))
        )
        self.assertEqual(released.status, DelegateRunStatus.RELEASED)
        self.assertEqual(released.delegate_run_id, envelope.delegate_run_id)
        self.assertFalse(self._tool_result(second, "pr-1")["is_error"])
        # The release still surfaces as a lifecycle event, with envelope-only
        # trace metadata now that the agent spec is gone.
        released_events = [e for e in second if e["event"] == "delegate_released"]
        self.assertEqual(len(released_events), 1)
        payload = released_events[0]["payload"]
        self.assertEqual(payload["delegate_run_id"], envelope.delegate_run_id)
        self.assertEqual(payload["agent_name"], "child")
        self.assertEqual(payload["delegated_by"], "parent")
        # The parent stream completed normally.
        self.assertTrue([e for e in second if e["event"] == "final"])

    async def test_stateful_start_raw_store_failure_returns_error_tool_result(self) -> None:
        """A raw (non-ApplicationError) failure inside coordinator.start must
        not kill the parent run — the stream survives with an error tool
        result, matching the legacy delegate path's blanket containment."""
        fixture = self._build(
            parent_responses=[],
            child_responses=[text_response("Child answer")],
            parent_model=ScriptedModelAdapter([
                tool_call_response("agent__child", arguments={"input": "Do it"}, call_id="pc-1"),
                text_response("Parent survived"),
            ]),
        )

        async def _boom(**_kwargs: object) -> None:
            raise RuntimeError("store exploded")

        fixture.service.start = _boom  # type: ignore[method-assign]

        events = await self._collect(
            fixture.runtime, fixture.parent, "Run",
            RunContext(agent_name="parent", session_id="s1", execution_scope_id="scope-1"),
        )
        result = self._tool_result(events, "pc-1")
        self.assertTrue(result["is_error"])
        self.assertIn("store exploded", str(result["content"]))
        self.assertTrue([e for e in events if e["event"] == "final"])

    async def test_delegate_turn_heartbeats_run_activity(self) -> None:
        """As child events stream past, the runtime refreshes the run's
        last_activity_at (bounded to one touch per heartbeat interval), so a
        legitimately-running turn longer than the running lease is never
        failed as an orphan by the parent's next maintenance sweep."""
        fixture = self._build(
            parent_responses=[],
            child_responses=[text_response("Child answer")],
            parent_model=ScriptedModelAdapter([
                tool_call_response("agent__child", arguments={"input": "Long job"}, call_id="pc-1"),
                text_response("Parent done"),
            ]),
        )
        touched: list[str] = []
        original_touch = fixture.service.touch_activity

        async def _recording(run_id: str) -> None:
            touched.append(run_id)
            await original_touch(run_id)

        fixture.service.touch_activity = _recording  # type: ignore[method-assign]

        original_interval = react_module.DELEGATE_HEARTBEAT_INTERVAL_SECONDS
        react_module.DELEGATE_HEARTBEAT_INTERVAL_SECONDS = 0.0  # touch on every event
        try:
            events = await self._collect(
                fixture.runtime, fixture.parent, "Run it",
                RunContext(agent_name="parent", session_id="s1", execution_scope_id="scope-1"),
            )
        finally:
            react_module.DELEGATE_HEARTBEAT_INTERVAL_SECONDS = original_interval

        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)
        self.assertIn(envelope.delegate_run_id, touched)
        self.assertGreaterEqual(touched.count(envelope.delegate_run_id), 1)

    async def test_lifecycle_handle_never_stringifies_into_tool_result(self) -> None:
        """Defense in depth: in a mis-wired runtime (lifecycle tools registered
        but no coordinator), the parent never had the lifecycle tools exposed,
        so a delegate_send call is denied by the request-level allowlist before
        any handler runs — and the raw DelegateRunHandle can never reach the
        transcript. (The generic-path stringify guard itself is unit-covered in
        tests/test_tool_allowlist.py.)"""
        session_store = InMemorySessionStore()
        run_store = InMemoryDelegateRunStore()
        parent = make_test_agent(name="parent", model="m-parent").model_copy(
            update={"delegate_agents": ["child"]}
        )
        child = make_test_agent(name="child", model="m-child")
        parent_model = ScriptedModelAdapter([
            tool_call_response(
                "delegate_send",
                arguments={"delegate_run_id": "placeholder", "input": "answer"},
                call_id="ps-1",
            ),
            text_response("Done"),
        ])
        registry = make_test_registry(parent, model=parent_model)
        registry.register_agent(child)
        register_ask_parent_tool(registry)
        service = DelegateService(
            registry=registry, run_store=run_store, settings=AppSettings()
        )
        register_delegate_lifecycle_tools(registry, service)
        # Mis-wired assembly: the lifecycle tools exist on the registry but
        # the runtime has no coordinator to intercept them.
        runtime = ReactAgentRuntime(
            registry,
            session_store=session_store,
            memory_store=RuntimeMemoryAdapter(session_store, run_store),
            enable_llm_summarization=False,
        )

        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )
        handle = await service.start(
            actor=DelegateActor.from_context(parent_context),
            delegate_agent_name="child",
            input_text="task",
            origin_tool_call_id=None,
            parent_context=parent_context,
        )
        await service.report_outcome(
            handle.run.id,
            DelegateTurnOutcome(
                status="waiting_parent",
                request=ParentInputRequest(
                    id="q1",
                    delegate_run_id=handle.run.id,
                    tool_call_id="ask-1",
                    title="Need target",
                ),
            ),
        )
        # Swap in the real scripted calls now that the run id is known.
        registry.model_providers[parent.provider.cache_key()] = ScriptedModelAdapter([
            tool_call_response(
                "delegate_send",
                arguments={"delegate_run_id": handle.run.id, "input": "answer"},
                call_id="ps-1",
            ),
            text_response("Done"),
        ])

        events = await self._collect(runtime, parent, "Send", parent_context)

        result = self._tool_result(events, "ps-1")
        self.assertTrue(result["is_error"])
        self.assertIn("not available", str(result["content"]))
        self.assertTrue([e for e in events if e["event"] == "final"])

    async def test_delegate_send_followup_to_idle_appends_parent_user_message(self) -> None:
        fixture = self._build(
            parent_responses=[],
            child_responses=[text_response("First answer"), text_response("Follow-up handled")],
            parent_model=_EnvelopeRunIdAdapter([
                tool_call_response(
                    "agent__child", arguments={"input": "Prepare the release"}, call_id="pc-1"
                ),
                tool_call_response(
                    "delegate_send",
                    arguments={"delegate_run_id": "", "input": "Now also run the tests"},
                    call_id="ps-1",
                ),
                text_response("All set"),
            ]),
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Ship it",
            RunContext(agent_name="parent", session_id="s1", execution_scope_id="scope-1"),
        )

        started = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        followup = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "ps-1"))
        )
        self.assertEqual(followup.delegate_run_id, started.delegate_run_id)
        self.assertEqual(followup.status, DelegateRunStatus.IDLE)
        self.assertEqual(followup.output, "Follow-up handled")

        # The follow-up landed as a parent-authored user message in the
        # child's mailbox and reached its second model call.
        second_messages = fixture.child_model.received_requests[1].messages
        followup_message = next(
            m for m in second_messages
            if m.role == "user" and "Now also run the tests" in str(m.content)
        )
        self.assertEqual(
            str(followup_message.content),
            "[message from parent agent 'parent']\n\nNow also run the tests",
        )
        row = await fixture.run_store.get_run(started.delegate_run_id)
        self.assertEqual(row.status, DelegateRunStatus.IDLE)

    async def test_delegate_list_returns_only_direct_children_compact(self) -> None:
        fixture = self._build(
            parent_responses=[],
            child_responses=[
                tool_call_response(
                    "agent__grand", arguments={"input": "nested job"}, call_id="cc-1"
                ),
                text_response("Child done"),
            ],
            parent_model=_EnvelopeRunIdAdapter([
                tool_call_response("agent__child", arguments={"input": "Build it"}, call_id="pc-1"),
                tool_call_response("delegate_list", arguments={}, call_id="pl-1"),
                text_response("Listed"),
            ]),
            child_delegate_agents=["grand"],
            extra_agents={"grand": ScriptedModelAdapter([text_response("Grand done")])},
        )
        parent_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )

        events = await self._collect(fixture.runtime, fixture.parent, "Build", parent_context)

        child_envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(child_envelope.status, DelegateRunStatus.IDLE)

        listed = json.loads(self._tool_result_content(events, "pl-1"))
        self.assertEqual(len(listed), 1)
        entry = listed[0]
        self.assertEqual(
            set(entry),
            {"delegate_run_id", "agent_name", "status", "last_activity_at", "summary"},
        )
        self.assertEqual(entry["agent_name"], "child")
        self.assertEqual(entry["delegate_run_id"], child_envelope.delegate_run_id)
        self.assertEqual(entry["status"], "idle")

        # The grandchild run exists (the child delegated on its own) but is
        # not among the parent's direct children.
        grandchildren = await fixture.run_store.list_children(
            execution_scope_id="scope-1",
            parent_agent_name="child",
            parent_delegate_run_id=child_envelope.delegate_run_id,
        )
        self.assertEqual(len(grandchildren), 1)
        self.assertEqual(grandchildren[0].delegate_agent_name, "grand")
        self.assertNotIn(grandchildren[0].id, {e["delegate_run_id"] for e in listed})

    async def test_delegate_release_then_send_gone(self) -> None:
        fixture = self._build(
            parent_responses=[],
            child_responses=[text_response("Child answer")],
            parent_model=_EnvelopeRunIdAdapter([
                tool_call_response("agent__child", arguments={"input": "One job"}, call_id="pc-1"),
                tool_call_response(
                    "delegate_release",
                    arguments={"delegate_run_id": "", "reason": "finished"},
                    call_id="pr-1",
                ),
                tool_call_response(
                    "delegate_send",
                    arguments={"delegate_run_id": "", "input": "one more"},
                    call_id="ps-1",
                ),
                text_response("Cleaned up"),
            ]),
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Run and clean up",
            RunContext(agent_name="parent", session_id="s1", execution_scope_id="scope-1"),
        )

        released = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pr-1"))
        )
        self.assertEqual(released.status, DelegateRunStatus.RELEASED)
        self.assertFalse(self._tool_result(events, "pr-1")["is_error"])
        row = await fixture.run_store.get_run(released.delegate_run_id)
        self.assertEqual(row.status, DelegateRunStatus.RELEASED)

        # The release surfaced as a lifecycle event carrying the reason.
        self.assertEqual(
            _lifecycle_sequence(events),
            ["delegate_created", "delegate_running", "delegate_idle", "delegate_released"],
        )
        released_payload = next(
            e["payload"] for e in events if e["event"] == "delegate_released"
        )
        self.assertEqual(released_payload["delegate_run_id"], released.delegate_run_id)
        self.assertEqual(released_payload["status"], "released")
        self.assertEqual(released_payload["summary"], "Released: finished")

        gone = self._tool_result(events, "ps-1")
        self.assertTrue(gone["is_error"])
        self.assertIn("released", str(gone["content"]).lower())

    async def test_sibling_cannot_send_via_tool(self) -> None:
        fixture = self._build(
            parent_responses=[
                tool_call_response("agent__child", arguments={"input": "Owned job"}, call_id="pc-1"),
                text_response("Started"),
            ],
            child_responses=[text_response("Child answer")],
        )
        owner_context = RunContext(
            agent_name="parent", session_id="s1", execution_scope_id="scope-1"
        )
        await self._collect(fixture.runtime, fixture.parent, "Delegate", owner_context)
        children = await fixture.service.list_children(actor=DelegateActor.from_context(owner_context))
        self.assertEqual(len(children), 1)
        run_id = children[0]["delegate_run_id"]

        stranger = make_test_agent(name="stranger", model="m-stranger")
        fixture.registry.register_agent(stranger)
        stranger_model = ScriptedModelAdapter([
            tool_call_response(
                "delegate_send",
                arguments={"delegate_run_id": run_id, "input": "hijack"},
                call_id="sp-1",
            ),
            text_response("Stranger done"),
        ])
        fixture.registry.model_providers[stranger.provider.cache_key()] = stranger_model

        events = await self._collect(
            fixture.runtime, stranger, "Try to reach it",
            RunContext(agent_name="stranger", session_id="s2", execution_scope_id="scope-2"),
        )

        rejected = self._tool_result(events, "sp-1")
        self.assertTrue(rejected["is_error"])
        # The stranger never had delegate_agents, so delegate_send was never
        # exposed to it — the request-level allowlist denies before the
        # ownership check inside the handler is even reached.
        self.assertIn("not available", str(rejected["content"]))

    async def test_delegate_send_child_context_propagates_execution_backend(self) -> None:
        """Both context-building legs (start and send) share one builder that
        inherits the parent's execution_backend, so workspace tools resolve
        identically inside the child."""
        fixture = self._build(
            parent_responses=[text_response("unused")],
            child_responses=[text_response("Child answer")],
        )
        backend = object()
        parent_context = RunContext(
            agent_name="parent",
            session_id="s1",
            execution_scope_id="scope-1",
            execution_backend=backend,
        )
        actor = DelegateActor.from_context(parent_context)

        started = await fixture.service.start(
            actor=actor,
            delegate_agent_name="child",
            input_text="first task",
            origin_tool_call_id=None,
            parent_context=parent_context,
        )
        self.assertIs(started.context.execution_backend, backend)
        await fixture.service.report_outcome(
            started.run.id, DelegateTurnOutcome(status="idle", output="done")
        )

        resumed = await fixture.service.send(
            actor=actor, delegate_run_id=started.run.id, input_text="again"
        )
        self.assertIs(resumed.context.execution_backend, backend)


def _delegate_tool_name(delegate_name: str) -> str:
    return f"agent__{delegate_name}"


class DelegateToolNameSanitizationTests(unittest.IsolatedAsyncioTestCase):
    """OpenAI-compatible providers reject function.name that is not
    ^[a-zA-Z0-9_-]+$. Delegate tools are built as ``agent__<name>`` from raw
    delegate_agents config; a dotted, spaced, or non-ASCII agent name must be
    sanitized in the model-facing tool name, and the runtime must resolve a
    sanitized tool_call back to the original delegate name."""

    def _register_all(self, delegate_agents: list[str], agent: AgentSpec) -> FrameworkRegistry:
        registry = make_test_registry(agent, model=ScriptedModelAdapter([]))
        for delegate_name in delegate_agents:
            child = make_test_agent(name=delegate_name, model=f"m-{delegate_name}")
            registry.register_agent(child)
            registry.model_providers[child.provider.cache_key()] = ScriptedModelAdapter([])
        return registry

    async def test_spaced_delegate_name_is_sanitized_in_tool_name(self) -> None:
        agent = make_test_agent(name="parent").model_copy(update={"delegate_agents": ["Random Speech Maker"]})
        runtime = make_test_runtime(self._register_all(["Random Speech Maker"], agent))
        tools = runtime._build_delegate_tools(agent)
        name = tools[0]["function"]["name"]
        self.assertRegex(name, r"^[a-zA-Z0-9_-]+$")
        self.assertEqual(name, "agent__Random-Speech-Maker")
        self.assertNotIn(" ", name)

    async def test_clean_delegate_name_is_unchanged(self) -> None:
        name = self._clean_delegate_tool_name("child")
        self.assertEqual(name, "agent__child")

    def _clean_delegate_tool_name(self, delegate_name: str) -> str:
        agent = make_test_agent(name="parent").model_copy(update={"delegate_agents": [delegate_name]})
        runtime = make_test_runtime(self._register_all([delegate_name], agent))
        return runtime._build_delegate_tools(agent)[0]["function"]["name"]

    async def test_normalize_resolves_sanitized_call_back_to_original_name(self) -> None:
        agent = make_test_agent(name="parent").model_copy(update={"delegate_agents": ["Random Speech Maker"]})
        runtime = make_test_runtime(self._register_all(["Random Speech Maker"], agent))
        self.assertEqual(
            runtime._normalize_delegate_tool_name(agent, "agent__Random-Speech-Maker"),
            "agent__Random Speech Maker",
        )

    async def test_collided_sanitized_names_get_distinct_suffixes(self) -> None:
        agent = make_test_agent(name="parent").model_copy(update={"delegate_agents": ["a.b", "a b", "a_b"]})
        runtime = make_test_runtime(self._register_all(["a.b", "a b", "a_b"], agent))
        names = {t["function"]["name"] for t in runtime._build_delegate_tools(agent)}
        self.assertEqual(len(names), 3, "collided names must stay distinct")
        for name in names:
            self.assertRegex(name, r"^agent__[a-zA-Z0-9_-]+$")
        # Round-trips: each sanitized tool name resolves back to a delegate.
        for name in names:
            resolved = runtime._normalize_delegate_tool_name(agent, name)
            self.assertIn(resolved.removeprefix("agent__"), {"a.b", "a b", "a_b"})

    async def test_model_echo_of_sanitized_name_runs_the_delegate(self) -> None:
        """The model only ever sees the sanitized tool name (agent__Random-Speech-
        Maker). When it echoes that name back, the runtime must resolve it to the
        original delegate and run it — the end-to-end shape of the reported 400."""
        parent = make_test_agent(name="parent", model="m-parent").model_copy(
            update={"delegate_agents": ["Random Speech Maker"]}
        )
        child = make_test_agent(name="Random Speech Maker", model="m-child")
        parent_adapter = ScriptedModelAdapter([
            tool_call_response("agent__Random-Speech-Maker", arguments={"input": "say it"}, call_id="dc-1"),
            text_response("Delegated."),
        ])
        child_adapter = ScriptedModelAdapter([text_response("hello from the delegate")])
        registry = make_test_registry(parent, model=parent_adapter)
        registry.register_agent(child)
        registry.model_providers[child.provider.cache_key()] = child_adapter
        runtime = make_test_runtime(registry)

        final = await runtime.run(parent, "go", RunContext(agent_name="parent", session_id="s1"))

        self.assertEqual(final.output_text, "Delegated.")
        # The sanitized tool name is what reached the model.
        sent_names = {t["function"]["name"] for t in parent_adapter.received_requests[0].tools}
        self.assertIn("agent__Random-Speech-Maker", sent_names)
        self.assertTrue(all(re.match(r"^agent__[a-zA-Z0-9_-]+$", n) for n in sent_names))
        # The delegate actually ran.
        self.assertEqual(child_adapter.call_count, 1)


if __name__ == "__main__":
    unittest.main()
