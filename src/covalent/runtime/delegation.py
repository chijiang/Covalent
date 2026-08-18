"""Stateful delegate lifecycle contract shared by runtime and application.

This module is the protocol seam: the runtime (ReAct loop) speaks
:class:`DelegateCoordinator` to drive delegate lifecycles without knowing
about persistence, while ``DelegateService`` (application layer) implements
the protocol on top of the delegate run store. Only core types are imported
at runtime; anything from the application or infra layers appears strictly
under ``TYPE_CHECKING`` so this package stays dependency-clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from covalent.core.types import DelegateRunResult, ParentInputRequest, RunContext

if TYPE_CHECKING:
    from covalent.core.agent import AgentSpec
    from covalent.infra.delegate_repository import DelegateRunRecord


@dataclass(frozen=True)
class DelegateActor:
    """Identity of the logical actor operating on delegate runs.

    Derived from :class:`RunContext` (never from model arguments): a root
    actor has ``delegate_run_id=None``; a delegate actor's id is its own run.
    """

    agent_name: str
    delegate_run_id: str | None  # None = root actor
    parent_delegate_run_id: str | None
    execution_scope_id: str | None
    session_id: str | None
    workspace_id: str | None
    memory_mode: str  # propagated to children

    @classmethod
    def from_context(cls, context: RunContext) -> "DelegateActor":
        return cls(
            agent_name=context.agent_name,
            delegate_run_id=context.delegate_run_id,
            parent_delegate_run_id=context.parent_delegate_run_id,
            execution_scope_id=context.execution_scope_id,
            session_id=context.session_id,
            workspace_id=context.workspace_id,
            memory_mode=context.memory_mode,
        )


@dataclass(frozen=True)
class DelegateRunHandle:
    """Everything the runtime needs to execute one child turn.

    ``run`` is in ``RUNNING`` state (the coordinator owns admission), and
    ``initial_input`` is ``""`` when resuming — the resume payload has already
    been appended to the run's memory by the coordinator.
    """

    run: "DelegateRunRecord"  # in RUNNING state
    agent: "AgentSpec"
    context: RunContext  # child context (memory scope + resume identity set)
    initial_input: str  # "" when resuming (input already in memory)


@dataclass(frozen=True)
class DelegateTurnOutcome:
    """Terminal report of one executed child turn."""

    status: Literal["idle", "waiting_parent", "failed", "cancelled"]
    output: str = ""
    request: ParentInputRequest | None = None
    error: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DelegateCoordinator(Protocol):
    """Lifecycle operations for stateful delegate runs.

    Implementations own admission (quotas, depth, delegate-edge validation),
    optimistic transitions, and per-run mailboxes. ``max_depth`` caps the
    delegation-chain length (0 = unlimited) and is read by the runtime's
    loop-depth check.
    """

    max_depth: int

    async def start(
        self,
        *,
        actor: DelegateActor,
        delegate_agent_name: str,
        input_text: str,
        origin_tool_call_id: str | None,
        parent_context: RunContext,
    ) -> DelegateRunHandle: ...

    async def report_outcome(
        self, run_id: str, outcome: DelegateTurnOutcome
    ) -> DelegateRunResult: ...

    async def send(
        self, *, actor: DelegateActor, delegate_run_id: str, input_text: str
    ) -> DelegateRunHandle: ...

    async def list_children(self, *, actor: DelegateActor) -> list[dict[str, Any]]: ...

    async def release(
        self, *, actor: DelegateActor, delegate_run_id: str, reason: str = ""
    ) -> DelegateRunResult: ...

    async def release_for_session(self, session_id: str, *, reason: str) -> int: ...

    async def finalize_scope(self, execution_scope_id: str, *, reason: str) -> int:
        """Mark active runs in the scope released; stateless rows (session_id
        NULL) are deleted outright after marking."""
        ...

    async def recover_orphans(self, *, now: datetime | None = None) -> list[str]:
        """Fail RUNNING rows whose lease lapsed: CAS to FAILED with
        ``{"code": "execution_interrupted"}`` (losers skip; idempotent)."""
        ...

    async def expire_stale(self, *, now: datetime | None = None) -> list[str]:
        """Expire IDLE/WAITING_PARENT/CREATED rows past their TTLs (and
        cascade to descendants), deleting their messages."""
        ...
