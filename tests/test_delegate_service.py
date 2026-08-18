"""DelegateService lifecycle tests.

One test per locked behavior in the task brief: delegate-edge validation,
quotas/depth, CAS transitions with fresh version reads, ownership tuples,
mailbox semantics per state, idempotent release, scope finalization, orphan
recovery, and TTL expiry with descendant cascade + retention deletion.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from covalent.application.errors import (
    DelegateConcurrentModificationError,
    DelegateDefinitionError,
    DelegateOwnershipError,
    DelegateQuotaError,
    DelegateRunGoneError,
    DelegateRunNotFoundError,
    DelegateTransitionError,
    InvalidInputError,
)
from covalent.application.services.delegate_service import (
    DelegateService,
    register_ask_parent_tool,
)
from covalent.core.types import (
    DelegateRunStatus,
    Message,
    ParentInputRequest,
    RunContext,
    ToolCall,
)
from covalent.infra.delegate_repository import (
    DelegateRunRecord,
    InMemoryDelegateRunStore,
)
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.delegation import (
    DelegateActor,
    DelegateCoordinator,
    DelegateTurnOutcome,
)
from tests.helpers import make_test_agent


def _parent_context(**metadata: object) -> RunContext:
    merged: dict[str, object] = {"memory_mode": "session"}
    merged.update(metadata)
    return RunContext(
        agent_name="parent",
        session_id="sess-1",
        execution_scope_id="scope-1",
        workspace_scope_id="ws-1",
        workspace_id="wk-1",
        metadata=merged,
    )


class DelegateServiceTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self) -> FrameworkRegistry:
        registry = FrameworkRegistry()
        parent = make_test_agent(name="parent").model_copy(
            update={"delegate_agents": ["child"]}
        )
        child = make_test_agent(name="child").model_copy(
            update={"delegate_agents": ["grand"]}
        )
        registry.register_agent(parent)
        registry.register_agent(child)
        registry.register_agent(make_test_agent(name="grand"))
        return registry

    def _service(
        self, **settings_overrides: object
    ) -> tuple[DelegateService, InMemoryDelegateRunStore]:
        settings = AppSettings(**settings_overrides) if settings_overrides else AppSettings()
        store = InMemoryDelegateRunStore()
        service = DelegateService(
            registry=self._registry(), run_store=store, settings=settings
        )
        return service, store

    def _root_actor(self) -> DelegateActor:
        return DelegateActor.from_context(_parent_context())

    def _start(self, service: DelegateService, **overrides: object):
        values: dict[str, object] = dict(
            actor=self._root_actor(),
            delegate_agent_name="child",
            input_text="do the thing",
            origin_tool_call_id="c1",
            parent_context=_parent_context(),
        )
        values.update(overrides)
        return service.start(**values)

    async def _raw_record(
        self, store: InMemoryDelegateRunStore, **overrides: object
    ) -> DelegateRunRecord:
        now = datetime.now(UTC)
        values: dict[str, object] = dict(
            id="run-raw",
            session_id="sess-1",
            execution_scope_id="scope-1",
            workspace_scope_id="ws-1",
            workspace_id="wk-1",
            root_agent_name="parent",
            parent_agent_name="parent",
            parent_delegate_run_id=None,
            delegate_agent_name="child",
            status=DelegateRunStatus.CREATED,
            version=1,
            created_at=now,
            last_activity_at=now,
        )
        values.update(overrides)
        return await store.create_run(DelegateRunRecord(**values))

    # --- behavior 1 + 2: start happy path, capsule, child context ----------

    async def test_start_creates_running_run_capsule_and_child_context(self) -> None:
        service, store = self._service()

        actor = DelegateActor.from_context(_parent_context())
        self.assertEqual(actor.agent_name, "parent")
        self.assertIsNone(actor.delegate_run_id)
        self.assertEqual(actor.execution_scope_id, "scope-1")
        self.assertEqual(actor.memory_mode, "session")

        handle = await self._start(service)

        self.assertEqual(handle.run.status, DelegateRunStatus.RUNNING)
        self.assertEqual(handle.run.delegate_agent_name, "child")
        self.assertEqual(handle.run.parent_agent_name, "parent")
        self.assertIsNone(handle.run.parent_delegate_run_id)
        self.assertEqual(handle.run.root_agent_name, "parent")
        self.assertEqual(handle.run.origin_tool_call_id, "c1")
        self.assertEqual(handle.run.session_id, "sess-1")
        self.assertEqual(handle.run.execution_scope_id, "scope-1")
        self.assertEqual(handle.run.workspace_scope_id, "ws-1")
        self.assertEqual(handle.run.workspace_id, "wk-1")
        self.assertEqual(handle.agent.name, "child")
        self.assertEqual(handle.initial_input, "do the thing")

        context = handle.context
        self.assertEqual(context.agent_name, "child")
        self.assertEqual(context.memory_scope_kind, "delegate")
        self.assertEqual(context.memory_scope_id, handle.run.id)
        self.assertEqual(context.delegate_run_id, handle.run.id)
        self.assertIsNone(context.parent_delegate_run_id)
        self.assertEqual(context.execution_scope_id, "scope-1")
        self.assertEqual(context.workspace_scope_id, "ws-1")
        self.assertEqual(context.workspace_id, "wk-1")
        self.assertEqual(context.metadata["delegation_chain"], ["parent"])
        self.assertEqual(context.metadata["delegated_by"], "parent")
        self.assertEqual(context.metadata["memory_mode"], "session")

        messages = await store.load_messages(handle.run.id)
        self.assertEqual(len(messages), 1)
        first = messages[0]
        self.assertEqual(first.role, "user")
        capsule = str(first.content)
        self.assertTrue(capsule.startswith("[delegation]"))
        self.assertIn(handle.run.id, capsule)
        self.assertIn("'parent'", capsule)
        self.assertIn("scope-1", capsule)
        self.assertIn("ask_parent", capsule)
        self.assertIn("do the thing", capsule)

    async def test_service_satisfies_coordinator_protocol(self) -> None:
        service, _ = self._service(delegate_max_depth=5)
        self.assertIsInstance(service, DelegateCoordinator)
        self.assertEqual(service.max_depth, 5)

    # --- behavior 1: definition validation ----------------------------------

    async def test_start_rejects_missing_or_unlisted_delegates(self) -> None:
        service, _ = self._service()

        with self.assertRaises(DelegateDefinitionError):
            await self._start(service, delegate_agent_name="stranger")

        stranger_actor = DelegateActor(
            agent_name="ghost",
            delegate_run_id=None,
            parent_delegate_run_id=None,
            execution_scope_id="scope-1",
            session_id="sess-1",
            workspace_id="wk-1",
            memory_mode="session",
        )
        with self.assertRaises(DelegateDefinitionError):
            await self._start(service, actor=stranger_actor)

        # Listed in delegate_agents but absent from the registry == disabled.
        registry = FrameworkRegistry()
        parent = make_test_agent(name="parent").model_copy(
            update={"delegate_agents": ["child"]}
        )
        registry.register_agent(parent)
        settings = AppSettings()
        orphan_service = DelegateService(
            registry=registry, run_store=InMemoryDelegateRunStore(), settings=settings
        )
        with self.assertRaises(DelegateDefinitionError):
            await self._start(orphan_service)

    # --- behavior 1: quotas and depth ---------------------------------------

    async def test_start_enforces_active_parent_scope_and_depth_quotas(self) -> None:
        per_parent, _ = self._service(delegate_max_active_runs_per_parent=1)
        await self._start(per_parent)
        with self.assertRaises(DelegateQuotaError):
            await self._start(per_parent)

        per_scope, _ = self._service(delegate_max_runs_per_scope=1)
        await self._start(per_scope)
        with self.assertRaises(DelegateQuotaError):
            await self._start(per_scope)

        depth, _ = self._service(delegate_max_depth=1)
        handle = await self._start(depth)
        child_actor = DelegateActor.from_context(handle.context)
        with self.assertRaises(DelegateQuotaError):
            await depth.start(
                actor=child_actor,
                delegate_agent_name="grand",
                input_text="nested",
                origin_tool_call_id=None,
                parent_context=handle.context,
            )

    # --- behavior 3: report_outcome mapping ---------------------------------

    async def test_report_outcome_maps_statuses_and_errors(self) -> None:
        service, store = self._service()
        handle = await self._start(service)

        idle = await service.report_outcome(
            handle.run.id, DelegateTurnOutcome(status="idle", output="all done")
        )
        self.assertEqual(idle.status, DelegateRunStatus.IDLE)
        self.assertEqual(idle.delegate_run_id, handle.run.id)
        self.assertEqual(idle.agent_name, "child")
        self.assertEqual(idle.output, "all done")
        stored = await store.get_run(handle.run.id)
        self.assertEqual(stored.status, DelegateRunStatus.IDLE)
        self.assertEqual(stored.latest_output, "all done")
        self.assertIsNotNone(stored.expires_at)

        waiting_handle = await self._start(service)
        request = ParentInputRequest(
            id="q1",
            delegate_run_id=waiting_handle.run.id,
            tool_call_id="ask-1",
            title="Need target",
        )
        waiting = await service.report_outcome(
            waiting_handle.run.id,
            DelegateTurnOutcome(status="waiting_parent", output="partial", request=request),
        )
        self.assertEqual(waiting.status, DelegateRunStatus.WAITING_PARENT)
        self.assertEqual(waiting.request, request)
        stored = await store.get_run(waiting_handle.run.id)
        self.assertEqual(stored.pending_request, request)

        failed_handle = await self._start(service)
        failed = await service.report_outcome(
            failed_handle.run.id,
            DelegateTurnOutcome(status="failed", error={"code": "boom"}),
        )
        self.assertEqual(failed.status, DelegateRunStatus.FAILED)
        self.assertEqual(failed.error, {"code": "boom"})

        cancelled_handle = await self._start(service)
        cancelled = await service.report_outcome(
            cancelled_handle.run.id, DelegateTurnOutcome(status="cancelled")
        )
        self.assertEqual(cancelled.status, DelegateRunStatus.CANCELLED)

        with self.assertRaises(DelegateRunNotFoundError):
            await service.report_outcome("delegate-missing", DelegateTurnOutcome(status="idle"))

        # Second report on an already-idle run is a concurrent modification.
        with self.assertRaises(DelegateConcurrentModificationError):
            await service.report_outcome(
                handle.run.id, DelegateTurnOutcome(status="idle", output="again")
            )

    # --- behavior 4: send to WAITING_PARENT (verbatim from the brief) -------

    async def test_send_to_waiting_appends_tool_result_matching_child_tool_call_id(
        self,
    ) -> None:
        service, store = self._service()
        handle = await self._start(service, input_text="task")
        request = ParentInputRequest(
            id="q1",
            delegate_run_id=handle.run.id,
            tool_call_id="ask-1",
            title="Need target",
        )
        await service.report_outcome(
            handle.run.id, DelegateTurnOutcome(status="waiting_parent", request=request)
        )
        resumed = await service.send(
            actor=self._root_actor(), delegate_run_id=handle.run.id, input_text="Use Docker"
        )
        messages = await store.load_messages(handle.run.id)
        self.assertEqual(messages[-1].role, "tool")
        self.assertEqual(messages[-1].name, "ask_parent")
        self.assertEqual(messages[-1].tool_call_id, "ask-1")
        self.assertEqual(resumed.run.status, DelegateRunStatus.RUNNING)
        self.assertIsNone(resumed.run.pending_request)
        self.assertEqual(messages[-1].content, json.dumps({"answers": "Use Docker"}))
        self.assertEqual(resumed.initial_input, "")

    # --- behavior 4: send to IDLE --------------------------------------------

    async def test_send_to_idle_appends_parent_user_message(self) -> None:
        service, store = self._service()
        handle = await self._start(service)
        await service.report_outcome(
            handle.run.id, DelegateTurnOutcome(status="idle", output="waiting for more")
        )
        resumed = await service.send(
            actor=self._root_actor(), delegate_run_id=handle.run.id, input_text="next step"
        )
        messages = await store.load_messages(handle.run.id)
        self.assertEqual(messages[-1].role, "user")
        self.assertEqual(messages[-1].content, "[message from parent agent 'parent']\n\nnext step")
        self.assertEqual(resumed.run.status, DelegateRunStatus.RUNNING)
        self.assertEqual(resumed.context.delegate_run_id, handle.run.id)
        self.assertEqual(resumed.context.metadata["delegation_chain"], ["parent"])

    # --- behavior 4: busy/terminal/missing/cap ------------------------------

    async def test_send_to_running_conflicts_and_terminal_gone(self) -> None:
        service, _ = self._service()
        handle = await self._start(service)
        with self.assertRaises(DelegateTransitionError):
            await service.send(
                actor=self._root_actor(), delegate_run_id=handle.run.id, input_text="hi"
            )
        await service.report_outcome(
            handle.run.id, DelegateTurnOutcome(status="cancelled")
        )
        with self.assertRaises(DelegateRunGoneError):
            await service.send(
                actor=self._root_actor(), delegate_run_id=handle.run.id, input_text="hi"
            )
        with self.assertRaises(DelegateRunNotFoundError):
            await service.send(
                actor=self._root_actor(), delegate_run_id="delegate-missing", input_text="hi"
            )

    async def test_send_enforces_message_cap(self) -> None:
        service, _ = self._service(delegate_max_messages_per_run=1)
        handle = await self._start(service)  # capsule+task is message #1
        await service.report_outcome(handle.run.id, DelegateTurnOutcome(status="idle"))
        with self.assertRaises(DelegateQuotaError):
            await service.send(
                actor=self._root_actor(), delegate_run_id=handle.run.id, input_text="more"
            )

    # --- behavior 4/5: ownership --------------------------------------------

    async def test_wrong_parent_cannot_send_or_release(self) -> None:
        service, _ = self._service()
        handle = await self._start(service)
        await service.report_outcome(handle.run.id, DelegateTurnOutcome(status="idle"))
        sibling = DelegateActor(
            agent_name="parent",
            delegate_run_id="delegate-someone-else",
            parent_delegate_run_id=None,
            execution_scope_id="scope-1",
            session_id="sess-1",
            workspace_id="wk-1",
            memory_mode="session",
        )
        with self.assertRaises(DelegateOwnershipError):
            await service.send(
                actor=sibling, delegate_run_id=handle.run.id, input_text="hi"
            )
        with self.assertRaises(DelegateOwnershipError):
            await service.release(actor=sibling, delegate_run_id=handle.run.id)

    # --- behavior 5: release idempotent --------------------------------------

    async def test_release_is_idempotent_and_keeps_messages(self) -> None:
        service, store = self._service()
        handle = await self._start(service)

        released = await service.release(
            actor=self._root_actor(), delegate_run_id=handle.run.id, reason="user_request"
        )
        self.assertEqual(released.status, DelegateRunStatus.RELEASED)
        stored = await store.get_run(handle.run.id)
        self.assertEqual(stored.release_reason, "user_request")
        self.assertIsNotNone(stored.released_at)
        self.assertNotEqual(await store.load_messages(handle.run.id), [])

        again = await service.release(
            actor=self._root_actor(), delegate_run_id=handle.run.id, reason="second"
        )
        self.assertEqual(again.status, DelegateRunStatus.RELEASED)
        again_stored = await store.get_run(handle.run.id)
        self.assertEqual(again_stored.released_at, stored.released_at)
        self.assertEqual(again_stored.release_reason, "user_request")

        with self.assertRaises(DelegateRunNotFoundError):
            await service.release(
                actor=self._root_actor(), delegate_run_id="delegate-missing"
            )

    # --- behavior 6: list_children -------------------------------------------

    async def test_list_children_compact_and_scoped(self) -> None:
        service, _ = self._service()
        first = await self._start(service)
        second = await self._start(service, input_text="second task")

        child_actor = DelegateActor.from_context(first.context)
        grandchild = await service.start(
            actor=child_actor,
            delegate_agent_name="grand",
            input_text="nested",
            origin_tool_call_id=None,
            parent_context=first.context,
        )

        children = await service.list_children(actor=self._root_actor())
        self.assertEqual(len(children), 2)
        for entry in children:
            self.assertEqual(
                set(entry),
                {"delegate_run_id", "agent_name", "status", "last_activity_at", "summary"},
            )
            self.assertEqual(entry["agent_name"], "child")
            self.assertEqual(entry["status"], "running")
            datetime.fromisoformat(entry["last_activity_at"])
        ids = {entry["delegate_run_id"] for entry in children}
        self.assertEqual(ids, {first.run.id, second.run.id})

        grandchildren = await service.list_children(actor=child_actor)
        self.assertEqual(len(grandchildren), 1)
        self.assertEqual(grandchildren[0]["delegate_run_id"], grandchild.run.id)
        self.assertEqual(grandchildren[0]["agent_name"], "grand")

    # --- behavior 7: finalize_scope ------------------------------------------

    async def test_finalize_scope_releases_and_deletes_stateless_rows(self) -> None:
        service, store = self._service()
        stateless_context = RunContext(
            agent_name="parent",
            session_id=None,
            execution_scope_id="scope-stateless",
            workspace_scope_id="ws-stateless",
            metadata={"memory_mode": "session"},
        )
        stateless = await service.start(
            actor=DelegateActor.from_context(stateless_context),
            delegate_agent_name="child",
            input_text="stateless task",
            origin_tool_call_id=None,
            parent_context=stateless_context,
        )
        # A session-bound run sharing the finalized scope: released but kept.
        stateful_context = RunContext(
            agent_name="parent",
            session_id="sess-1",
            execution_scope_id="scope-stateless",
            workspace_scope_id="ws-stateless",
            metadata={"memory_mode": "session"},
        )
        stateful = await service.start(
            actor=DelegateActor.from_context(stateful_context),
            delegate_agent_name="child",
            input_text="stateful task",
            origin_tool_call_id=None,
            parent_context=stateful_context,
        )
        other_scope = await self._start(service)

        count = await service.finalize_scope("scope-stateless", reason="invoke_complete")
        self.assertEqual(count, 2)
        self.assertIsNone(await store.get_run(stateless.run.id))
        self.assertEqual(await store.load_messages(stateless.run.id), [])

        kept = await store.get_run(stateful.run.id)
        self.assertEqual(kept.status, DelegateRunStatus.RELEASED)
        self.assertEqual(kept.release_reason, "invoke_complete")
        # Stateful rows keep their messages for retention to handle later.
        self.assertNotEqual(await store.load_messages(stateful.run.id), [])

        untouched = await store.get_run(other_scope.run.id)
        self.assertEqual(untouched.status, DelegateRunStatus.RUNNING)

    # --- behavior 7: release_for_session --------------------------------------

    async def test_release_for_session_releases_active_runs(self) -> None:
        service, store = self._service()
        handle = await self._start(service)
        released = await service.release_for_session("sess-1", reason="session_closed")
        self.assertEqual(released, 1)
        stored = await store.get_run(handle.run.id)
        self.assertEqual(stored.status, DelegateRunStatus.RELEASED)
        self.assertEqual(stored.release_reason, "session_closed")
        # Idempotent: nothing active remains.
        self.assertEqual(await service.release_for_session("sess-1", reason="again"), 0)

    # --- behavior 7: recover_orphans ------------------------------------------

    async def test_recover_orphans_fails_stale_running(self) -> None:
        service, store = self._service(delegate_running_lease_seconds=900.0)
        handle = await self._start(service)

        self.assertEqual(await service.recover_orphans(now=datetime.now(UTC)), [])

        failed = await service.recover_orphans(
            now=datetime.now(UTC) + timedelta(seconds=1800)
        )
        self.assertEqual(failed, [handle.run.id])
        stored = await store.get_run(handle.run.id)
        self.assertEqual(stored.status, DelegateRunStatus.FAILED)
        self.assertEqual(stored.error, {"code": "execution_interrupted"})

        # Idempotent: the row is no longer RUNNING.
        self.assertEqual(
            await service.recover_orphans(now=datetime.now(UTC) + timedelta(seconds=3600)),
            [],
        )

    # --- behavior 7: expire_stale ----------------------------------------------

    async def test_expire_stale_ttl_cascade_and_retention(self) -> None:
        service, store = self._service(
            delegate_idle_ttl_seconds=3600.0,
            delegate_released_retention_seconds=600.0,
        )
        parent_run = await self._start(service)
        await service.report_outcome(
            parent_run.run.id, DelegateTurnOutcome(status="idle", output="done")
        )

        running_child = await self._raw_record(
            store,
            id="run-child",
            parent_agent_name="child",
            parent_delegate_run_id=parent_run.run.id,
            delegate_agent_name="grand",
            status=DelegateRunStatus.RUNNING,
        )
        await store.save_messages("run-child", [Message(role="user", content="child work")])

        released_grandchild = await self._raw_record(
            store,
            id="run-grandchild",
            parent_agent_name="grand",
            parent_delegate_run_id=running_child.id,
            delegate_agent_name="deep",
            status=DelegateRunStatus.RELEASED,
            released_at=datetime.now(UTC) - timedelta(hours=3),
        )
        await store.save_messages(
            "run-grandchild", [Message(role="user", content="grandchild work")]
        )

        now = datetime.now(UTC) + timedelta(hours=10)
        expired = await service.expire_stale(now=now)

        self.assertIn(parent_run.run.id, expired)
        self.assertIn(running_child.id, expired)
        # Descendants are expired before their parents.
        self.assertLess(expired.index(running_child.id), expired.index(parent_run.run.id))

        parent_after = await store.get_run(parent_run.run.id)
        self.assertEqual(parent_after.status, DelegateRunStatus.EXPIRED)
        child_after = await store.get_run(running_child.id)
        self.assertEqual(child_after.status, DelegateRunStatus.EXPIRED)
        self.assertEqual(await store.load_messages(parent_run.run.id), [])
        self.assertEqual(await store.load_messages(running_child.id), [])

        # Released rows past retention keep their status but lose messages.
        grandchild_after = await store.get_run(released_grandchild.id)
        self.assertEqual(grandchild_after.status, DelegateRunStatus.RELEASED)
        self.assertEqual(await store.load_messages(released_grandchild.id), [])

        # Idempotent: nothing left to expire.
        self.assertEqual(await service.expire_stale(now=now), [])

    async def test_expire_stale_disabled_ttl_never_expires(self) -> None:
        # idle TTL disabled (0): an IDLE row carrying a stale expires_at from
        # its waiting TTL must never be expired by the idle bucket.
        service, store = self._service(
            delegate_idle_ttl_seconds=0.0,
            delegate_waiting_ttl_seconds=3600.0,
        )
        handle = await self._start(service)
        await service.report_outcome(
            handle.run.id, DelegateTurnOutcome(status="idle", output="done")
        )
        stored = await store.get_run(handle.run.id)
        self.assertIsNotNone(stored.expires_at)

        expired = await service.expire_stale(now=datetime.now(UTC) + timedelta(hours=10))
        self.assertEqual(expired, [])
        stored = await store.get_run(handle.run.id)
        self.assertEqual(stored.status, DelegateRunStatus.IDLE)
        self.assertNotEqual(await store.load_messages(handle.run.id), [])


class AskParentToolTests(unittest.IsolatedAsyncioTestCase):
    """The ask_parent local tool: handler validation and registry promotion.

    The handler builds a ParentInputRequest only inside a delegated run; the
    registry's local-tool branch converts a returned ParentInputRequest into a
    pausing ToolResult (content "Waiting for parent") carrying the request.
    """

    @staticmethod
    def _registry() -> FrameworkRegistry:
        registry = FrameworkRegistry()
        register_ask_parent_tool(registry)
        return registry

    def test_handler_builds_parent_input_request_in_delegated_context(self) -> None:
        handler = self._registry().local_tools["ask_parent"].handler
        assert handler is not None

        request = handler(
            {
                "title": "Need target",
                "questions": [{"header": "Target", "question": "Which target?"}],
            },
            RunContext(agent_name="child", delegate_run_id="run-7"),
        )

        self.assertIsInstance(request, ParentInputRequest)
        self.assertEqual(request.delegate_run_id, "run-7")
        self.assertEqual(request.title, "Need target")
        self.assertEqual(request.questions[0].header, "Target")
        self.assertIsNone(request.tool_call_id, "registry promotion fills the tool call id")

    def test_handler_rejected_outside_delegated_run(self) -> None:
        handler = self._registry().local_tools["ask_parent"].handler
        assert handler is not None

        with self.assertRaises(InvalidInputError):
            handler({"title": "T"}, RunContext(agent_name="parent"))
        with self.assertRaises(InvalidInputError):
            handler({"title": "T"}, None)

    def test_handler_requires_title(self) -> None:
        handler = self._registry().local_tools["ask_parent"].handler
        assert handler is not None

        with self.assertRaises(InvalidInputError):
            handler({}, RunContext(agent_name="child", delegate_run_id="run-7"))

    async def test_registry_promotes_parent_input_request_to_tool_result(self) -> None:
        registry = self._registry()
        agent = make_test_agent(name="child")
        tool_call = ToolCall(id="tc-9", name="ask_parent", arguments={"title": "Need target"})

        result = await registry.execute_tool_call(
            agent, tool_call, RunContext(agent_name="child", delegate_run_id="run-7")
        )

        self.assertEqual(result.content, "Waiting for parent")
        self.assertFalse(result.is_error)
        self.assertIsNone(result.input_request)
        self.assertIsNotNone(result.parent_request)
        self.assertEqual(result.parent_request.tool_call_id, "tc-9")
        self.assertEqual(result.parent_request.delegate_run_id, "run-7")

    async def test_registry_converts_handler_rejection_to_error_result(self) -> None:
        registry = self._registry()
        agent = make_test_agent(name="parent")
        tool_call = ToolCall(id="tc-10", name="ask_parent", arguments={"title": "T"})

        result = await registry.execute_tool_call(
            agent, tool_call, RunContext(agent_name="parent")
        )

        self.assertTrue(result.is_error)
        self.assertIn("only available inside a delegated run", str(result.content))
        self.assertIsNone(result.parent_request)


if __name__ == "__main__":
    unittest.main()
