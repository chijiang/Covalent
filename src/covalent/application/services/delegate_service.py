"""Stateful delegate lifecycle service.

``DelegateService`` implements the :class:`~covalent.runtime.delegation.DelegateCoordinator`
protocol: admission (delegate edge, quotas, depth), optimistic state
transitions with fresh version reads, per-run mailboxes, scoped release, and
the maintenance sweeps (orphan recovery, TTL expiry). It is persistence and
lifecycle policy only — it never runs agents; the runtime consumes the
handles it returns.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from covalent.application._utils import _new_chat_item_id
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
from covalent.core.types import (
    DelegateRunResult,
    DelegateRunStatus,
    Message,
    ParentInputRequest,
    RunContext,
    UserQuestion,
)
from covalent.infra.delegate_repository import (
    ACTIVE_STATUSES,
    DelegateRunConflictError,
    DelegateRunMissingError,
    DelegateRunRecord,
    DelegateRunStore,
)
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.delegation import DelegateActor, DelegateRunHandle, DelegateTurnOutcome

logger = logging.getLogger(__name__)

#: Statuses after which a run can never act again (release is idempotent on them).
_TERMINAL_STATUSES: tuple[DelegateRunStatus, ...] = (
    DelegateRunStatus.RELEASED,
    DelegateRunStatus.CANCELLED,
    DelegateRunStatus.FAILED,
    DelegateRunStatus.EXPIRED,
)

#: Sentinel offset for "this TTL is disabled (0 = never expire)". The store
#: matches ``expires_at <= before``, so a disabled bucket needs an impossibly
#: OLD threshold (far past), never a future one.
_NEVER_DAYS = 100 * 365

#: Local tool name for pausing a delegated run to ask its parent.
ASK_PARENT_TOOL = "ask_parent"

_ASK_PARENT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ASK_PARENT_TOOL,
        "description": (
            "Pause this delegated run and ask your parent agent a question. Use it when "
            "you need information only the parent (or the end user, via the parent) can "
            "provide; never attempt to contact the end user directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short summary of what you need from the parent.",
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "header": {"type": "string"},
                            "question": {"type": "string"},
                            "message": {"type": "string"},
                            "multi_select": {"type": "boolean"},
                            "allow_freeform_input": {"type": "boolean"},
                            "max_selections": {"type": "integer", "minimum": 1},
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                        "recommended": {"type": "boolean"},
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["header", "question"],
                    },
                },
            },
            "required": ["title"],
        },
    },
}


def register_ask_parent_tool(registry: FrameworkRegistry) -> None:
    """Register the ask_parent local tool.

    The schema is appended to delegated contexts' tool lists by the runtime;
    the handler only succeeds inside a delegated run (``context.delegate_run_id``
    set). The registry's local-tool branch promotes the returned
    ``ParentInputRequest`` into a pausing ToolResult.
    """
    registry.register_local_tool(ASK_PARENT_TOOL, _ASK_PARENT_SCHEMA, handler=_ask_parent_handler)


def _ask_parent_handler(args: dict[str, Any], ctx: RunContext | None) -> ParentInputRequest:
    if ctx is None or not ctx.delegate_run_id:
        raise InvalidInputError(
            "ask_parent is only available inside a delegated run; "
            "in root context answer from your own context or tools"
        )
    title = str(args.get("title") or "").strip()
    if not title:
        raise InvalidInputError("ask_parent requires a non-empty 'title'")
    return ParentInputRequest(
        id=_new_chat_item_id("question"),
        delegate_run_id=ctx.delegate_run_id,
        # Left None here on purpose: handlers do not receive the ToolCall, and
        # the registry's promotion fills tool_call_id from the invoking call.
        tool_call_id=None,
        title=title,
        questions=[UserQuestion.model_validate(q) for q in args.get("questions", [])],
    )


#: Local tool names for the stateful delegate lifecycle (mirrored by the
#: runtime's DELEGATE_LIFECYCLE_TOOLS tuple). Exposed to root agents only.
DELEGATE_SEND_TOOL = "delegate_send"
DELEGATE_LIST_TOOL = "delegate_list"
DELEGATE_RELEASE_TOOL = "delegate_release"

_DELEGATE_SEND_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DELEGATE_SEND_TOOL,
        "description": (
            "Send input to one of your own delegate runs: answer a delegate that is "
            "waiting_parent on ask_parent (its request is in the envelope you received) "
            "or give follow-up work to an idle delegate. The run executes one turn and "
            "returns its JSON envelope. Operates only on delegate runs you created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delegate_run_id": {
                    "type": "string",
                    "description": "The delegate_run_id from a previous delegate envelope.",
                },
                "input": {
                    "type": "string",
                    "description": "Your answer or follow-up instruction for the delegate.",
                },
            },
            "required": ["delegate_run_id", "input"],
        },
    },
}

_DELEGATE_LIST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DELEGATE_LIST_TOOL,
        "description": (
            "List your own delegate runs (delegate_run_id, agent_name, status, "
            "last_activity_at, summary) so you can recover run ids before sending "
            "input or releasing them. Operates only on delegate runs you created."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_DELEGATE_RELEASE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": DELEGATE_RELEASE_TOOL,
        "description": (
            "Release one of your own delegate runs you no longer need. Idle runs stay "
            "alive (holding storage) until released, cancelled, or expired. Returns the "
            "run's final JSON envelope. Operates only on delegate runs you created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delegate_run_id": {
                    "type": "string",
                    "description": "The delegate_run_id from a previous delegate envelope.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason recorded on the release.",
                },
            },
            "required": ["delegate_run_id"],
        },
    },
}


def register_delegate_lifecycle_tools(
    registry: FrameworkRegistry, service: DelegateService
) -> None:
    """Register the delegate_send / delegate_list / delegate_release local tools.

    Handlers build :meth:`DelegateActor.from_context` from the calling context
    (never from model arguments), so every operation is scoped to the caller's
    own delegates; typed application errors surface as error tool results via
    the registry. ``delegate_send`` returns the resumed
    :class:`~covalent.runtime.delegation.DelegateRunHandle` itself — a
    non-string marker the runtime detects to drive the child turn and replace
    the result with the run's JSON envelope (the service stays runtime-free);
    ``list``/``release`` return plain JSON strings.
    """

    def _actor(ctx: RunContext | None) -> DelegateActor:
        if ctx is None:
            raise InvalidInputError(
                "delegate lifecycle tools require a run context; they operate "
                "on the caller's own delegate runs"
            )
        return DelegateActor.from_context(ctx)

    def _run_id(args: dict[str, Any]) -> str:
        run_id = str(args.get("delegate_run_id") or "").strip()
        if not run_id:
            raise InvalidInputError("a non-empty 'delegate_run_id' is required")
        return run_id

    async def _send_handler(args: dict[str, Any], ctx: RunContext | None) -> DelegateRunHandle:
        input_text = str(args.get("input") or "").strip()
        if not input_text:
            raise InvalidInputError("delegate_send requires a non-empty 'input'")
        return await service.send(
            actor=_actor(ctx), delegate_run_id=_run_id(args), input_text=input_text
        )

    async def _list_handler(args: dict[str, Any], ctx: RunContext | None) -> str:
        children = await service.list_children(actor=_actor(ctx))
        return json.dumps(children, ensure_ascii=False)

    async def _release_handler(args: dict[str, Any], ctx: RunContext | None) -> str:
        result = await service.release(
            actor=_actor(ctx),
            delegate_run_id=_run_id(args),
            reason=str(args.get("reason") or ""),
        )
        return result.model_dump_json()

    registry.register_local_tool(
        DELEGATE_SEND_TOOL, _DELEGATE_SEND_SCHEMA, handler=_send_handler
    )
    registry.register_local_tool(
        DELEGATE_LIST_TOOL, _DELEGATE_LIST_SCHEMA, handler=_list_handler
    )
    registry.register_local_tool(
        DELEGATE_RELEASE_TOOL, _DELEGATE_RELEASE_SCHEMA, handler=_release_handler
    )


class DelegateService:
    """Coordinator for stateful delegate runs, backed by a ``DelegateRunStore``.

    Records returned by the store are never mutated in place — every derived
    view uses ``model_copy`` and every transition re-reads for a fresh
    version number before the next CAS.
    """

    def __init__(
        self,
        *,
        registry: FrameworkRegistry,
        run_store: DelegateRunStore,
        settings: AppSettings,
    ) -> None:
        self._registry = registry
        self._store = run_store
        self._settings = settings
        self.max_depth = settings.delegate_max_depth

    # --- helpers -------------------------------------------------------------

    def _log_transition(
        self,
        run: DelegateRunRecord,
        transition: str,
        *,
        parent_agent_name: str | None = None,
        session_id: str | None = None,
    ) -> None:
        logger.info(
            "delegate_run_transition",
            extra={
                "delegate_run_id": run.id,
                "transition": transition,
                "parent_agent_name": parent_agent_name
                if parent_agent_name is not None
                else run.parent_agent_name,
                "session_id": session_id if session_id is not None else run.session_id,
            },
        )

    def _envelope(self, run: DelegateRunRecord) -> DelegateRunResult:
        return DelegateRunResult(
            delegate_run_id=run.id,
            agent_name=run.delegate_agent_name,
            status=run.status,
            output=run.latest_output,
            request=run.pending_request,
            error=run.error,
        )

    def _check_ownership(self, run: DelegateRunRecord, actor: DelegateActor) -> None:
        ownership = (run.parent_agent_name, run.parent_delegate_run_id, run.execution_scope_id)
        expected = (actor.agent_name, actor.delegate_run_id, actor.execution_scope_id)
        if ownership != expected:
            raise DelegateOwnershipError(
                f"delegate run {run.id} is not owned by actor "
                f"{actor.agent_name}/{actor.delegate_run_id}"
            )

    async def _transition(
        self,
        run: DelegateRunRecord,
        transition: str,
        *,
        from_statuses: tuple[DelegateRunStatus, ...] | None = None,
        **updates: Any,
    ) -> DelegateRunRecord:
        """CAS from the given source statuses (default: the run's current
        status); maps neutral store errors to application errors."""
        try:
            updated = await self._store.transition_run(
                run.id,
                expected_version=run.version,
                from_statuses=from_statuses or (run.status,),
                **updates,
            )
        except DelegateRunMissingError as exc:
            raise DelegateRunNotFoundError(f"delegate run {run.id} not found") from exc
        except DelegateRunConflictError as exc:
            raise DelegateConcurrentModificationError(
                f"delegate run {run.id} changed concurrently during {transition}"
            ) from exc
        self._log_transition(updated, transition)
        return updated

    def _ttl(self, seconds: float) -> timedelta | None:
        return timedelta(seconds=seconds) if seconds > 0 else None

    async def _chain_for_run(self, run: DelegateRunRecord) -> list[str]:
        """Reconstruct the delegation chain (root agent first) by walking
        parent run links. Bounded by chain depth; missing parents just end
        the walk."""
        names: list[str] = []
        seen = {run.id}
        current = run
        while True:
            names.insert(0, current.parent_agent_name)
            parent_id = current.parent_delegate_run_id
            if parent_id is None or parent_id in seen:
                break
            parent = await self._store.get_run(parent_id)
            if parent is None:
                break
            seen.add(parent.id)
            current = parent
        return names

    def _child_context(
        self,
        run: DelegateRunRecord,
        *,
        chain: list[str],
        delegated_by: str,
        memory_mode: str,
        execution_backend: Any = None,
    ) -> RunContext:
        """The single child-context builder for both the start and send legs.

        Scope identity comes from the run record (resolved, and exactly what
        ownership checks compare against); the execution backend is inherited
        from the acting parent context so workspace tools resolve identically
        inside the child. ``sandbox_instance_id`` is deliberately NOT set —
        each agent resolves its own logical sandbox on its first run.
        """
        return RunContext(
            agent_name=run.delegate_agent_name,
            session_id=run.session_id,
            memory_scope_kind="delegate",
            memory_scope_id=run.id,
            delegate_run_id=run.id,
            parent_delegate_run_id=run.parent_delegate_run_id,
            execution_scope_id=run.execution_scope_id,
            workspace_scope_id=run.workspace_scope_id,
            workspace_id=run.workspace_id,
            execution_backend=execution_backend,
            metadata={
                "delegation_chain": list(chain),
                "delegated_by": delegated_by,
                "memory_mode": memory_mode,
            },
        )

    # --- DelegateCoordinator: start ------------------------------------------

    async def start(
        self,
        *,
        actor: DelegateActor,
        delegate_agent_name: str,
        input_text: str,
        origin_tool_call_id: str | None,
        parent_context: RunContext,
    ) -> DelegateRunHandle:
        # Delegate edge: the parent must exist, list the delegate, and the
        # delegate must be registered (absent == disabled at runtime).
        try:
            parent_spec = self._registry.get_agent(actor.agent_name)
        except KeyError as exc:
            raise DelegateDefinitionError(
                f"unknown parent agent '{actor.agent_name}'"
            ) from exc
        if delegate_agent_name not in parent_spec.delegate_agents:
            raise DelegateDefinitionError(
                f"agent '{actor.agent_name}' may not delegate to '{delegate_agent_name}'"
            )
        try:
            delegate_spec = self._registry.get_agent(delegate_agent_name)
        except KeyError as exc:
            raise DelegateDefinitionError(
                f"unknown or disabled delegate agent '{delegate_agent_name}'"
            ) from exc

        raw_chain = parent_context.metadata.get("delegation_chain")
        chain = [str(name) for name in raw_chain] if isinstance(raw_chain, list) else []

        now = datetime.now(UTC)
        run_id = _new_chat_item_id("delegate")
        execution_scope_id = (
            getattr(parent_context, "execution_scope_id", None)
            or parent_context.session_id
            or run_id
        )
        workspace_scope_id = (
            getattr(parent_context, "workspace_scope_id", None) or execution_scope_id
        )

        # Quotas (checked before any row is written).
        active = await self._store.count_active_by_parent(
            execution_scope_id=execution_scope_id,
            parent_agent_name=actor.agent_name,
            parent_delegate_run_id=actor.delegate_run_id,
        )
        if active >= self._settings.delegate_max_active_runs_per_parent:
            raise DelegateQuotaError(
                f"agent '{actor.agent_name}' already has {active} active delegate runs "
                f"(max {self._settings.delegate_max_active_runs_per_parent})"
            )
        scope_count = await self._store.count_by_execution_scope(execution_scope_id)
        if scope_count >= self._settings.delegate_max_runs_per_scope:
            raise DelegateQuotaError(
                f"execution scope {execution_scope_id} already holds {scope_count} "
                f"delegate runs (max {self._settings.delegate_max_runs_per_scope})"
            )
        depth = len(chain) + 1
        if self._settings.delegate_max_depth > 0 and depth > self._settings.delegate_max_depth:
            raise DelegateQuotaError(
                f"delegation depth {depth} exceeds max {self._settings.delegate_max_depth}"
            )

        waiting_ttl = self._ttl(self._settings.delegate_waiting_ttl_seconds)
        record = DelegateRunRecord(
            id=run_id,
            session_id=parent_context.session_id,
            execution_scope_id=execution_scope_id,
            workspace_scope_id=workspace_scope_id,
            workspace_id=getattr(parent_context, "workspace_id", None),
            root_agent_name=chain[0] if chain else actor.agent_name,
            parent_agent_name=actor.agent_name,
            parent_delegate_run_id=actor.delegate_run_id,
            delegate_agent_name=delegate_agent_name,
            origin_tool_call_id=origin_tool_call_id,
            status=DelegateRunStatus.CREATED,
            version=1,
            created_at=now,
            last_activity_at=now,
            expires_at=(now + waiting_ttl) if waiting_ttl else None,
        )
        created = await self._store.create_run(record)

        capsule = (
            f"[delegation] You are delegate run {created.id} of agent "
            f"'{created.delegate_agent_name}'. "
            f"Your direct parent is agent '{created.parent_agent_name}'. "
            f"Shared execution scope: {created.execution_scope_id}; "
            f"shared workspace scope: {created.workspace_scope_id}. "
            "You do not see the parent's conversation history — only the task below "
            "and later messages from your parent. "
            "If you need information only the parent has, call ask_parent with a "
            "concrete question; never ask the end user directly. "
            "Files you create belong in the shared session workspace."
        )
        await self._store.save_messages(
            created.id, [Message(role="user", content=f"{capsule}\n\n{input_text}")]
        )

        running = await self._transition(
            created, "created->running", status=DelegateRunStatus.RUNNING
        )

        child_context = self._child_context(
            running,
            chain=[*chain, actor.agent_name],
            delegated_by=actor.agent_name,
            memory_mode=actor.memory_mode,
            execution_backend=getattr(parent_context, "execution_backend", None),
        )
        return DelegateRunHandle(
            run=running, agent=delegate_spec, context=child_context, initial_input=input_text
        )

    # --- DelegateCoordinator: report_outcome ----------------------------------

    async def report_outcome(self, run_id: str, outcome: DelegateTurnOutcome) -> DelegateRunResult:
        run = await self._store.get_run(run_id)
        if run is None:
            raise DelegateRunNotFoundError(f"delegate run {run_id} not found")
        now = datetime.now(UTC)
        if outcome.status == "idle":
            idle_ttl = self._ttl(self._settings.delegate_idle_ttl_seconds)
            updated = await self._transition(
                run,
                "running->idle",
                from_statuses=(DelegateRunStatus.RUNNING,),
                status=DelegateRunStatus.IDLE,
                latest_output=outcome.output,
                expires_at=(now + idle_ttl) if idle_ttl else None,
            )
        elif outcome.status == "waiting_parent":
            if outcome.request is None:
                raise DelegateTransitionError(
                    f"waiting_parent outcome for {run_id} requires a request"
                )
            waiting_ttl = self._ttl(self._settings.delegate_waiting_ttl_seconds)
            updated = await self._transition(
                run,
                "running->waiting_parent",
                from_statuses=(DelegateRunStatus.RUNNING,),
                status=DelegateRunStatus.WAITING_PARENT,
                pending_request=outcome.request,
                latest_output=outcome.output,
                expires_at=(now + waiting_ttl) if waiting_ttl else None,
            )
        elif outcome.status == "failed":
            updated = await self._transition(
                run,
                "running->failed",
                from_statuses=(DelegateRunStatus.RUNNING,),
                status=DelegateRunStatus.FAILED,
                error=outcome.error,
            )
        else:  # cancelled
            updated = await self._transition(
                run,
                "running->cancelled",
                from_statuses=(DelegateRunStatus.RUNNING,),
                status=DelegateRunStatus.CANCELLED,
            )
        return self._envelope(updated)

    # --- DelegateCoordinator: send ---------------------------------------------

    async def send(
        self, *, actor: DelegateActor, delegate_run_id: str, input_text: str
    ) -> DelegateRunHandle:
        run = await self._store.get_run(delegate_run_id)
        if run is None:
            raise DelegateRunNotFoundError(f"delegate run {delegate_run_id} not found")
        self._check_ownership(run, actor)

        if run.status in _TERMINAL_STATUSES:
            raise DelegateRunGoneError(
                f"delegate run {delegate_run_id} is {run.status.value}"
            )
        if run.status in (DelegateRunStatus.RUNNING, DelegateRunStatus.CREATED):
            raise DelegateTransitionError(
                f"delegate run {delegate_run_id} is {run.status.value}; "
                "no concurrent mailbox"
            )

        if await self._store.count_messages(run.id) >= self._settings.delegate_max_messages_per_run:
            raise DelegateQuotaError(
                f"delegate run {run.id} reached its message cap "
                f"({self._settings.delegate_max_messages_per_run})"
            )

        messages = await self._store.load_messages(run.id)
        if run.status == DelegateRunStatus.WAITING_PARENT and run.pending_request is not None:
            messages.append(
                Message(
                    role="tool",
                    name="ask_parent",
                    tool_call_id=run.pending_request.tool_call_id,
                    content=json.dumps({"answers": input_text}),
                )
            )
            resumed = await self._transition(
                run,
                "waiting_parent->running",
                status=DelegateRunStatus.RUNNING,
                clear_pending=True,
            )
        else:
            messages.append(
                Message(
                    role="user",
                    content=f"[message from parent agent '{actor.agent_name}']\n\n{input_text}",
                )
            )
            resumed = await self._transition(
                run, f"{run.status.value}->running", status=DelegateRunStatus.RUNNING
            )

        await self._store.save_messages(run.id, messages)
        chain = await self._chain_for_run(resumed)
        context = self._child_context(
            resumed,
            chain=chain,
            delegated_by=actor.agent_name,
            memory_mode=actor.memory_mode,
            execution_backend=actor.execution_backend,
        )
        try:
            agent = self._registry.get_agent(resumed.delegate_agent_name)
        except KeyError as exc:
            raise DelegateDefinitionError(
                f"unknown or disabled delegate agent '{resumed.delegate_agent_name}'"
            ) from exc
        return DelegateRunHandle(
            run=resumed, agent=agent, context=context, initial_input=""
        )

    # --- DelegateCoordinator: list_children / release ---------------------------

    async def list_children(self, *, actor: DelegateActor) -> list[dict[str, Any]]:
        records = await self._store.list_children(
            execution_scope_id=actor.execution_scope_id or "",
            parent_agent_name=actor.agent_name,
            parent_delegate_run_id=actor.delegate_run_id,
        )
        return [
            {
                "delegate_run_id": record.id,
                "agent_name": record.delegate_agent_name,
                "status": record.status.value,
                "last_activity_at": record.last_activity_at.isoformat(),
                "summary": record.summary,
            }
            for record in records
        ]

    async def release(
        self, *, actor: DelegateActor, delegate_run_id: str, reason: str = ""
    ) -> DelegateRunResult:
        run = await self._store.get_run(delegate_run_id)
        if run is None:
            raise DelegateRunNotFoundError(f"delegate run {delegate_run_id} not found")
        self._check_ownership(run, actor)
        if run.status in _TERMINAL_STATUSES:
            return self._envelope(run)  # idempotent no-op
        updated = await self._transition(
            run,
            f"{run.status.value}->released",
            status=DelegateRunStatus.RELEASED,
            release_reason=reason,
            released_at=datetime.now(UTC),
        )
        return self._envelope(updated)

    # --- DelegateCoordinator: bulk release / sweeps ------------------------------

    async def release_for_session(self, session_id: str, *, reason: str) -> int:
        active = await self._store.list_active_by_session(session_id)
        if not active:
            return 0
        released_at = datetime.now(UTC)
        scope_ids = {record.execution_scope_id for record in active}
        total = 0
        for scope_id in sorted(scope_ids):
            total += await self._store.release_scope(
                scope_id, reason=reason, released_at=released_at
            )
        for record in active:
            self._log_transition(record, "active->released", session_id=session_id)
        return total

    async def finalize_scope(self, execution_scope_id: str, *, reason: str) -> int:
        active = await self._store.list_active_by_execution_scope(execution_scope_id)
        stateless_ids = [record.id for record in active if record.session_id is None]
        released = await self._store.release_scope(
            execution_scope_id, reason=reason, released_at=datetime.now(UTC)
        )
        for record in active:
            self._log_transition(record, "active->released")
        # Stateless runs have no session to outlive — remove them outright
        # (delete_run cascades to their descendants and messages).
        for run_id in stateless_ids:
            await self._store.delete_run(run_id)
        return released

    async def recover_orphans(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        older_than = current - timedelta(seconds=self._settings.delegate_running_lease_seconds)
        stale = await self._store.list_stale_running(older_than=older_than)
        failed_ids: list[str] = []
        for record in stale:
            try:
                updated = await self._store.transition_run(
                    record.id,
                    expected_version=record.version,
                    from_statuses=(DelegateRunStatus.RUNNING,),
                    status=DelegateRunStatus.FAILED,
                    error={"code": "execution_interrupted"},
                )
            except (DelegateRunConflictError, DelegateRunMissingError):
                continue  # resumed or deleted concurrently — idempotent skip
            self._log_transition(updated, "running->failed(recovered)")
            failed_ids.append(updated.id)
        return failed_ids

    async def expire_stale(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        never_before = current - timedelta(days=_NEVER_DAYS)
        idle_ttl = self._settings.delegate_idle_ttl_seconds
        waiting_ttl = self._settings.delegate_waiting_ttl_seconds
        # 0 disables a bucket: with an impossibly old threshold no row's
        # expires_at can satisfy ``expires_at <= before``, so that bucket
        # never expires (even rows carrying a stale expires_at from a
        # previous state's TTL).
        idle_before = (
            current - timedelta(seconds=idle_ttl) if idle_ttl > 0 else never_before
        )
        waiting_before = (
            current - timedelta(seconds=waiting_ttl) if waiting_ttl > 0 else never_before
        )
        candidates = await self._store.list_expirable(
            idle_before=idle_before, waiting_before=waiting_before
        )
        expired_ids: list[str] = []
        seen: set[str] = set()
        for record in candidates:
            if record.id in seen:
                continue
            seen.add(record.id)
            # Descendants first (deepest first) so parents never expire while
            # a live child still points at them.
            descendants = await self._descendants(record, seen)
            for row in [*descendants, record]:
                if await self._expire_row(row, current):
                    expired_ids.append(row.id)
        return expired_ids

    async def _descendants(
        self, root: DelegateRunRecord, seen: set[str]
    ) -> list[DelegateRunRecord]:
        """Deepest-first descendant closure via ``list_children`` per row."""
        ordered: list[DelegateRunRecord] = []
        frontier = [root]
        while frontier:
            current = frontier.pop()
            children = await self._store.list_children(
                execution_scope_id=current.execution_scope_id,
                parent_agent_name=current.delegate_agent_name,
                parent_delegate_run_id=current.id,
            )
            for child in children:
                if child.id in seen:
                    continue
                seen.add(child.id)
                ordered.insert(0, child)
                frontier.append(child)
        return ordered

    async def _expire_row(self, row: DelegateRunRecord, now: datetime) -> bool:
        """Expire one row via CAS; also applies the released-retention sweep.

        Returns True when this call performed the EXPIRED transition. Rows
        already terminal (a released descendant caught in the cascade) keep
        their status but lose their messages once the released-retention
        window has passed.
        """
        try:
            updated = await self._store.transition_run(
                row.id,
                expected_version=row.version,
                from_statuses=ACTIVE_STATUSES,
                status=DelegateRunStatus.EXPIRED,
            )
        except (DelegateRunConflictError, DelegateRunMissingError):
            updated = None  # moved on / deleted concurrently — idempotent skip
        if updated is not None:
            self._log_transition(updated, f"{row.status.value}->expired")
            await self._store.delete_messages(updated.id)
        if (
            row.released_at is not None
            and row.released_at + timedelta(seconds=self._settings.delegate_released_retention_seconds)
            < now
        ):
            await self._store.delete_messages(row.id)
        return updated is not None
