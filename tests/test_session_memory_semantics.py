"""Model-memory semantics: transcript edits and delegate persistence.

Guards the two session-memory invariants the UI depends on:

1. Editing/resending (PUT /transcript) must change what the MODEL sees next —
   previously only the visible transcript changed while ``memory_messages``
   kept the removed tail.
2. Concurrent delegates share the parent conversation READ-ONLY — their
   intermediate context is never persisted, so they cannot overwrite each
   other, the parent's turn, or leak into later requests.
"""

from __future__ import annotations

import asyncio
import unittest

from covalent.core.types import Message, RunContext
from covalent.infra.memory import InMemorySessionStore
from covalent.registry.registry import FrameworkRegistry

from tests.helpers import (
    ScriptedModelAdapter,
    make_test_agent,
    text_response,
    tool_call_response,
)


def _runtime(store: InMemorySessionStore):
    from covalent.runtime.react import ReactAgentRuntime

    return ReactAgentRuntime(
        FrameworkRegistry(),
        session_store=store,
        session_history_limit=10,
        enable_llm_summarization=False,
    )


def _message(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


class TranscriptEditDrivesModelContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_messages_leave_model_memory(self) -> None:
        """The edit-and-resend route must rebuild MODEL memory from the
        surviving visible messages — previously only the transcript changed."""
        from datetime import UTC, datetime

        from starlette.testclient import TestClient

        from covalent.api.app import create_app
        from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage
        from covalent.infra.settings import AppSettings
        from types import SimpleNamespace

        from tests.test_session_transcript_replace_api import (
            _FakeDbSession,
            _admin_cookie,
        )

        store = InMemorySessionStore()
        await store.save_session(
            ChatSessionRecord(
                id="sess-edit",
                title="t",
                title_source="auto",
                agent_name="default",
                owner_user_id=None,
                workspace_id=None,
                preview_text="",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                memory_messages=[
                    _message("user", "first question"),
                    _message("assistant", "first answer"),
                    _message("user", "second question"),
                    _message("assistant", "second answer"),
                ],
                messages=[
                    ChatTranscriptMessage(id="um-1", role="user", content="first question"),
                    ChatTranscriptMessage(id="am-1", role="assistant", content="first answer"),
                    ChatTranscriptMessage(id="um-2", role="user", content="second question"),
                    ChatTranscriptMessage(id="am-2", role="assistant", content="second answer"),
                ],
                activity=[],
            )
        )
        app = create_app()
        app.state.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
        app.state.session_store = store
        client = TestClient(app)

        resp = client.put(
            "/sessions/sess-edit/transcript",
            json={"truncate_before_message_id": "um-2"},
            headers={"Cookie": _admin_cookie(app)},
        )
        self.assertEqual(resp.status_code, 200)

        reloaded = await store.load_messages("sess-edit")
        contents = " | ".join(str(m.content) for m in reloaded)
        self.assertNotIn("second question", contents)
        self.assertNotIn("second answer", contents)
        self.assertIn("first question", contents)
        self.assertIn("first answer", contents)

    async def test_next_model_request_excludes_removed_messages(self) -> None:
        """End to end: truncate, resend, and inspect the actual messages the
        model adapter received."""
        store = InMemorySessionStore()
        await store.save_messages(
            "sess-e2e",
            [
                _message("user", "first question"),
                _message("assistant", "first answer"),
                _message("user", "stale question"),
                _message("assistant", "stale answer"),
            ],
        )
        # Simulate the route's truncation of model memory to the first pair.
        await store.save_messages(
            "sess-e2e",
            [_message("user", "first question"), _message("assistant", "first answer")],
        )

        model = ScriptedModelAdapter([text_response("fresh answer")])
        agent = make_test_agent(name="test")
        registry = FrameworkRegistry()
        registry.register_agent(agent)
        registry.model_providers[agent.provider.cache_key()] = model
        from covalent.runtime.react import ReactAgentRuntime

        runtime = ReactAgentRuntime(
            registry, session_store=store, session_history_limit=10, enable_llm_summarization=False
        )
        context = RunContext(agent_name="test", session_id="sess-e2e", execution_scope_id="sess-e2e")
        response = await runtime.run(agent, "corrected question", context)

        self.assertEqual(response.output_text, "fresh answer")
        seen = [str(m.content) for request in model.received_requests for m in request.messages]
        self.assertTrue(any("stale question" not in item and "corrected question" in item for item in seen))
        self.assertFalse(any("stale question" in item or "stale answer" in item for item in seen))


class ConcurrentDelegatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_delegates_never_persist_their_context(self) -> None:
        """Two delegates run concurrently (gather); their internal turns must
        not land in session memory, and the parent's final turn must win."""
        store = InMemorySessionStore()
        await store.save_messages("sess-delegate", [_message("user", "original task")])

        master = make_test_agent(name="master", description="m", system_prompt="m")
        master = master.model_copy(update={"delegate_agents": ["w1", "w2"]})
        w1 = make_test_agent(name="w1", description="w1", system_prompt="w1")
        w2 = make_test_agent(name="w2", description="w2", system_prompt="w2")
        model = ScriptedModelAdapter(
            [
                # Master turn: delegate to both workers in one response.
                _multi_tool_response(
                    [("agent__w1", "task for w1", "c1"), ("agent__w2", "task for w2", "c2")]
                ),
                text_response("worker 1 answer"),  # w1 run
                text_response("worker 2 answer"),  # w2 run
                text_response("master final"),  # master final turn
            ]
        )
        registry = FrameworkRegistry()
        for spec in (master, w1, w2):
            registry.register_agent(spec)
        registry.model_providers[master.provider.cache_key()] = model

        from covalent.runtime.react import ReactAgentRuntime

        runtime = ReactAgentRuntime(
            registry, session_store=store, session_history_limit=10, enable_llm_summarization=False
        )
        context = RunContext(
            agent_name="master", session_id="sess-delegate", execution_scope_id="sess-delegate"
        )
        response = await runtime.run(master, "split the work", context)
        self.assertEqual(response.output_text, "master final")

        final = await store.load_messages("sess-delegate")
        contents = [str(m.content) for m in final]
        joined = " | ".join(contents)

        # The parent's conversation survives intact.
        self.assertIn("original task", joined)
        self.assertIn("split the work", joined)
        self.assertIn("master final", joined)
        # Delegate internal turns never persisted (their answers reach the
        # parent via tool results, not by writing session memory).
        self.assertNotIn("task for w1", joined)
        self.assertNotIn("task for w2", joined)
        # No lost parent turn: the delegate tool calls and results are part of
        # the MASTER's transcript.
        self.assertTrue(
            any(m.role == "tool" and "worker 1 answer" in str(m.content) for m in final),
            f"expected delegate tool results in master transcript, got roles={[m.role for m in final]}",
        )


    async def test_delegate_context_residue_after_parent_failure(self) -> None:
        """Delegates complete, then the MASTER's next model call fails — the
        delegates' internal context must not linger in session memory."""
        from covalent.model.base import ModelProviderError
        from covalent.runtime.react import ReactAgentRuntime

        store = InMemorySessionStore()
        master = make_test_agent(name="master", description="m", system_prompt="m")
        master = master.model_copy(update={"delegate_agents": ["w1"]})
        w1 = make_test_agent(name="w1", description="w1", system_prompt="w1")
        model = _ExplodingAfterAdapter(
            [
                tool_call_response("agent__w1", arguments={"input": "secret task"}, call_id="c1"),
                text_response("worker internal answer"),  # w1 run
            ],
            explode_after=2,  # master's follow-up turn explodes
        )
        registry = FrameworkRegistry()
        for spec in (master, w1):
            registry.register_agent(spec)
        registry.model_providers[master.provider.cache_key()] = model

        runtime = ReactAgentRuntime(
            registry, session_store=store, session_history_limit=10, enable_llm_summarization=False
        )
        context = RunContext(agent_name="master", session_id="sess-fail", execution_scope_id="sess-fail")
        with self.assertRaises(ModelProviderError):
            await runtime.run(master, "do the work", context)

        final = await store.load_messages("sess-fail")
        joined = " | ".join(str(m.content) for m in final)
        self.assertNotIn("secret task", joined, "delegate internal input leaked into session memory")
        self.assertNotIn("worker internal answer", joined, "delegate internal answer leaked into session memory")


class _ExplodingAfterAdapter(ScriptedModelAdapter):
    """Returns scripted responses until the Nth call, then raises."""

    def __init__(self, responses, *, explode_after: int) -> None:
        super().__init__(responses)
        self._explode_after = explode_after
        self._calls = 0

    async def generate(self, request):
        from covalent.model.base import ModelProviderError

        self._calls += 1
        if self._calls > self._explode_after:
            raise ModelProviderError("test", "provider exploded")
        return await super().generate(request)


def _multi_tool_response(calls: list[tuple[str, str, str]]):
    """A single model response carrying several tool calls at once."""
    import json

    from covalent.core.types import GenerationResponse, TokenUsage, ToolCall

    raw_calls = [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"input": text})},
        }
        for name, text, call_id in calls
    ]
    return GenerationResponse(
        output_text="",
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments={"input": text}, raw=raw)
            for (name, text, call_id), raw in zip(calls, raw_calls)
        ],
        assistant_message=Message(role="assistant", content="", tool_calls=raw_calls),
        usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
    )


if __name__ == "__main__":
    unittest.main()
