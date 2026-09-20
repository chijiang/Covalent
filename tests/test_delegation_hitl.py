"""End-to-end HITL escalation and resume for stateful delegate runs.

Drives the real runtime + DelegateService against the route-layer resume
contract (``routes/agents.py`` + ``session_service``):

1. a delegated child can never surface a user-input pause — its ``ask_user``
   call becomes an error tool result, and no ``delegate_input_required`` or
   child-originated root ``input_required`` ever reaches the stream;
2. root escalation: parent starts child -> child ``ask_parent`` -> parent's
   next response calls its own ``ask_user`` -> stream 1 ends with exactly ONE
   root ``input_required`` while the child row stays durably ``waiting_parent``
   -> stream 2 resumes with route-built ``resume_tool_result`` metadata ->
   parent ``delegate_send`` -> child resumes from its saved ``ask_parent``
   boundary -> parent final answer carries the child output;
3. process-restart reconstruction: stream 2 runs through a FRESH runtime +
   service + model adapters sharing only the persisted stores.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from covalent.application.services.delegate_service import (
    DelegateService,
    register_ask_parent_tool,
    register_delegate_lifecycle_tools,
)
from covalent.application.services.session_service import (
    AgentRunInput,
    _build_resume_tool_result,
    _extract_pending_user_input,
)
from covalent.core.types import (
    DelegateRunResult,
    DelegateRunStatus,
    GenerationResponse,
    RunContext,
    UserInputRequest,
    UserQuestion,
)
from covalent.infra.delegate_repository import InMemoryDelegateRunStore
from covalent.infra.memory import ChatActivityItem, InMemorySessionStore
from covalent.infra.settings import AppSettings
from covalent.runtime.memory_port import RuntimeMemoryAdapter
from covalent.runtime.react import ReactAgentRuntime

from tests.helpers import (
    _EnvelopeRunIdAdapter,
    ScriptedModelAdapter,
    make_test_agent,
    make_test_registry,
    text_response,
    tool_call_response,
)

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


def _fake_ask_user_handler(_args: dict[str, Any], _ctx: RunContext | None) -> UserInputRequest:
    """Mirrors management_service._ask_user_handler's shape with a fixed id so
    the pending-question round trip is deterministic."""
    return UserInputRequest(
        id="question-1",
        tool_name="ask_user",
        title="Which target?",
        questions=[UserQuestion(header="Target", question="Which deployment target?")],
    )


def _start_child_call(call_id: str = "pc-1") -> GenerationResponse:
    return tool_call_response(
        "agent__child", arguments={"input": "Deploy the service"}, call_id=call_id
    )


def _ask_parent_call(call_id: str = "ask-1") -> GenerationResponse:
    return tool_call_response(
        "ask_parent",
        arguments={
            "title": "Need target",
            "questions": [{"header": "Target", "question": "Which target?"}],
        },
        call_id=call_id,
    )


def _ask_user_call(call_id: str = "qu-1") -> GenerationResponse:
    return tool_call_response(
        "ask_user",
        arguments={
            "title": "Which target?",
            "questions": [{"header": "Target", "question": "Which deployment target?"}],
        },
        call_id=call_id,
    )


def _delegate_send_call(call_id: str = "ps-1") -> GenerationResponse:
    return tool_call_response(
        "delegate_send",
        arguments={"delegate_run_id": "", "input": "Use Docker"},
        call_id=call_id,
    )


def _route_resume_metadata(
    events: list[dict], *, answers: dict[str, Any], display_input: str
) -> dict[str, Any]:
    """Mirror routes/agents.py: derive the runtime metadata for the NEXT stream
    from the ``input_required`` this stream recorded as chat activity."""
    blocking = next(event["payload"] for event in events if event["event"] == "input_required")
    activity = [ChatActivityItem(id="act-input-1", title="input_required", payload=blocking)]
    pending = _extract_pending_user_input(activity)
    assert pending is not None
    resume = _build_resume_tool_result(
        AgentRunInput(
            input=display_input,
            metadata={
                "resume_question_id": pending.id,
                "question_response": answers,
            },
        ),
        pending,
    )
    return {"resume_tool_result": resume.model_dump(mode="json")}


class DelegateHitlEscalationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _build(
        *,
        parent_responses: list[GenerationResponse],
        child_responses: list[GenerationResponse],
        session_store: InMemorySessionStore | None = None,
        run_store: InMemoryDelegateRunStore | None = None,
        child_model: ScriptedModelAdapter | None = None,
    ) -> SimpleNamespace:
        session_store = session_store or InMemorySessionStore()
        run_store = run_store or InMemoryDelegateRunStore()
        # ask_user is a per-agent granted builtin in production (console
        # local_tools config); the parent must declare it for the execution-side
        # allowlist to expose/permit it.
        parent = make_test_agent(name="parent", model="m-parent", local_tools=["ask_user"]).model_copy(
            update={"delegate_agents": ["child"]}
        )
        child = make_test_agent(name="child", model="m-child")
        parent_adapter = _EnvelopeRunIdAdapter(parent_responses)
        child_adapter = child_model or ScriptedModelAdapter(child_responses)
        registry = make_test_registry(parent, model=parent_adapter)
        registry.register_agent(child)
        registry.model_providers[child.provider.cache_key()] = child_adapter
        register_ask_parent_tool(registry)
        registry.register_local_tool(
            "ask_user", _ASK_USER_SCHEMA, handler=_fake_ask_user_handler
        )
        service = DelegateService(
            registry=registry, run_store=run_store, settings=AppSettings()
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
            session_store=session_store,
            run_store=run_store,
            parent_model=parent_adapter,
            child_model=child_adapter,
        )

    @staticmethod
    async def _collect(runtime, agent, user_input, context) -> list[dict]:
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
    def _child_delegate_run_id(events: list[dict]) -> str:
        envelope = DelegateRunResult.model_validate(
            json.loads(DelegateHitlEscalationTests._tool_result_content(events, "pc-1"))
        )
        return envelope.delegate_run_id

    def _root_context(self, metadata: dict[str, Any] | None = None) -> RunContext:
        return RunContext(
            agent_name="parent",
            session_id="s1",
            execution_scope_id="scope-1",
            metadata=metadata or {},
        )

    async def test_delegated_ask_user_never_reaches_root_as_input_required(self) -> None:
        """A child calling ask_user by name is rejected server-side; the pause
        never surfaces as a stream boundary of any kind."""
        fixture = self._build(
            parent_responses=[_start_child_call(), text_response("Parent done")],
            child_responses=[
                tool_call_response(
                    "ask_user",
                    arguments={"questions": [{"header": "Target", "question": "Which?"}]},
                    call_id="au-1",
                ),
                text_response("Continued without user input"),
            ],
        )

        events = await self._collect(
            fixture.runtime, fixture.parent, "Run the child", self._root_context()
        )

        self.assertFalse(
            [e for e in events if e["event"] in ("input_required", "delegate_input_required", "parent_input_required")],
            "a child ask_user must never pause the parent stream",
        )
        converted = None
        for event in events:
            if event["event"] != "delegate_tool_results":
                continue
            for result in event["payload"]["results"]:
                if result.get("tool_call_id") == "au-1":
                    converted = result
        self.assertIsNotNone(converted, "expected the converted ask_user result in delegate traces")
        self.assertTrue(converted["is_error"])
        self.assertIn("ask_parent", str(converted["content"]))

        envelope = DelegateRunResult.model_validate(
            json.loads(self._tool_result_content(events, "pc-1"))
        )
        self.assertEqual(envelope.status, DelegateRunStatus.IDLE)

    async def test_root_escalation_and_resume_through_delegate_send(self) -> None:
        fixture = self._build(
            parent_responses=[
                _start_child_call(),
                _ask_user_call(),
                _delegate_send_call(),
                text_response("Deployment finished: Resolved with Docker"),
            ],
            child_responses=[_ask_parent_call(), text_response("Resolved with Docker")],
        )

        # Stream 1: child pauses on ask_parent, the parent escalates to the
        # end user with its own ask_user, and the root run pauses there.
        first = await self._collect(
            fixture.runtime, fixture.parent, "Deploy the service", self._root_context()
        )

        input_events = [e for e in first if e["event"] == "input_required"]
        self.assertEqual(len(input_events), 1, "stream 1 must end with exactly one root input_required")
        request = UserInputRequest.model_validate(input_events[0]["payload"])
        self.assertEqual(request.id, "question-1")
        self.assertEqual(request.tool_call_id, "qu-1")
        self.assertFalse(
            [e for e in first if e["event"] in ("delegate_input_required", "parent_input_required")],
            "the child pause must surface as an ordinary envelope, never as a boundary",
        )

        # The child row is durably WAITING_PARENT at its ask_parent boundary.
        run_id = self._child_delegate_run_id(first)
        row = await fixture.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.WAITING_PARENT)
        self.assertEqual(row.pending_request.tool_call_id, "ask-1")

        # Session memory holds exactly the parent transcript through the root
        # ask_user tool call — no delegate internals leak in.
        session_messages = await fixture.session_store.load_messages("s1")
        self.assertEqual(
            [m.role for m in session_messages], ["user", "assistant", "tool", "assistant"]
        )
        self.assertEqual(session_messages[-1].tool_calls[0]["id"], "qu-1")
        self.assertNotIn(
            "[delegation]", " ".join(str(m.content) for m in session_messages)
        )

        # Stream 2 (resume): route-layer metadata built exactly like
        # routes/agents.py — the parent answers the child with delegate_send.
        resume_context = self._root_context(
            _route_resume_metadata(first, answers={"Target": "Docker"}, display_input="Use Docker")
        )
        second = await self._collect(
            fixture.runtime, fixture.parent, "Use Docker", resume_context
        )

        # Exactly ONE root input_required across both streams; nothing else paused.
        self.assertEqual(
            sum(1 for e in [*first, *second] if e["event"] == "input_required"), 1
        )
        self.assertFalse([e for e in [*first, *second] if e["event"] == "delegate_input_required"])
        finals = [e for e in second if e["event"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("Resolved with Docker", finals[0]["payload"]["output_text"])

        # The parent's resumed request carries the injected ask_user tool message.
        resumed_request = fixture.parent_model.received_requests[2]
        self.assertEqual(resumed_request.messages[-1].role, "tool")
        self.assertEqual(resumed_request.messages[-1].tool_call_id, "qu-1")
        self.assertEqual(resumed_request.messages[-1].name, "ask_user")
        self.assertIn("Docker", str(resumed_request.messages[-1].content))

        # The child resumed from its saved ask_parent boundary with its
        # pre-pause memory intact (capsule + task + assistant ask_parent call).
        resumed_child_messages = fixture.child_model.received_requests[-1].messages
        self.assertEqual(resumed_child_messages[-1].role, "tool")
        self.assertEqual(resumed_child_messages[-1].tool_call_id, "ask-1")
        self.assertIn("Use Docker", str(resumed_child_messages[-1].content))
        self.assertIn(
            "[delegation]",
            " ".join(str(m.content) for m in resumed_child_messages if m.role == "user"),
        )

        # The delegate mailbox ends with the ask_parent tool call + matching
        # answer + the child's final assistant output. Exactly ONE tool result
        # exists for the ask_parent call: the placeholder at pause time is not
        # persisted (two tool messages sharing one tool_call_id would violate
        # the tool-call protocol).
        child_messages = await fixture.run_store.load_messages(run_id)
        ask_calls = [
            m
            for m in child_messages
            if m.role == "assistant"
            and any(
                isinstance(c, dict) and (c.get("function") or {}).get("name") == "ask_parent"
                for c in m.tool_calls
            )
        ]
        self.assertEqual(len(ask_calls), 1)
        self.assertEqual(ask_calls[0].tool_calls[0]["id"], "ask-1")
        ask_answers = [
            m for m in child_messages if m.role == "tool" and m.tool_call_id == "ask-1"
        ]
        self.assertEqual(len(ask_answers), 1)
        self.assertIn("Use Docker", str(ask_answers[0].content))
        self.assertEqual(child_messages[-1].role, "assistant")
        self.assertIn("Resolved with Docker", str(child_messages[-1].content))

        row = await fixture.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.IDLE)

    async def test_process_restart_resume_through_fresh_stack(self) -> None:
        """Stream 2 completes through freshly constructed runtime, service,
        registry, and model adapters sharing only the persisted stores — the
        WAITING_PARENT row, pending request, and memory drive the resume."""
        session_store = InMemorySessionStore()
        run_store = InMemoryDelegateRunStore()
        first_stack = self._build(
            parent_responses=[_start_child_call(), _ask_user_call()],
            child_responses=[_ask_parent_call()],
            session_store=session_store,
            run_store=run_store,
        )

        first = await self._collect(
            first_stack.runtime, first_stack.parent, "Deploy the service", self._root_context()
        )
        self.assertEqual(len([e for e in first if e["event"] == "input_required"]), 1)
        run_id = self._child_delegate_run_id(first)
        row = await first_stack.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.WAITING_PARENT)
        self.assertEqual(row.pending_request.tool_call_id, "ask-1")

        # A fresh "process": only the stores survive.
        second_stack = self._build(
            parent_responses=[_delegate_send_call(), text_response("Deployment finished: Resolved with Docker")],
            child_responses=[text_response("Resolved with Docker")],
            session_store=session_store,
            run_store=run_store,
        )
        self.assertIsNot(second_stack.runtime, first_stack.runtime)
        self.assertIsNot(second_stack.service, first_stack.service)

        resume_context = self._root_context(
            _route_resume_metadata(first, answers={"Target": "Docker"}, display_input="Use Docker")
        )
        second = await self._collect(
            second_stack.runtime, second_stack.parent, "Use Docker", resume_context
        )

        finals = [e for e in second if e["event"] == "final"]
        self.assertEqual(len(finals), 1)
        self.assertIn("Resolved with Docker", finals[0]["payload"]["output_text"])

        # Every fresh model call was stateless reconstruction from the stores.
        self.assertEqual(second_stack.parent_model.call_count, 2)
        self.assertEqual(second_stack.child_model.call_count, 1)
        resumed_child_messages = second_stack.child_model.received_requests[-1].messages
        self.assertEqual(resumed_child_messages[-1].role, "tool")
        self.assertEqual(resumed_child_messages[-1].tool_call_id, "ask-1")

        child_messages = await second_stack.run_store.load_messages(run_id)
        self.assertEqual(child_messages[-1].role, "assistant")
        self.assertIn("Resolved with Docker", str(child_messages[-1].content))
        row = await second_stack.run_store.get_run(run_id)
        self.assertEqual(row.status, DelegateRunStatus.IDLE)


if __name__ == "__main__":
    unittest.main()
