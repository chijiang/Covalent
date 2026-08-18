"""Runtime memory port — scope-routed memory access for agent runs.

The port decouples ``ReactAgentRuntime`` from concrete stores: memory
operations are expressed as ``(scope_kind, scope_id)`` pairs and routed by
kind — ``"session"`` to the :class:`~covalent.infra.memory.SessionStore`,
``"delegate"`` to the delegate run store, ``"none"`` to a no-op. The delegate
store is duck-typed and only imported for static type checking, so the
runtime package never depends on the concrete delegate repository at runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from covalent.core.types import Message
from covalent.infra.memory import SessionStore

if TYPE_CHECKING:
    # Type-only: keeps runtime free of a runtime-level dependency on the
    # concrete delegate repository (infra already depends on core only).
    from covalent.infra.delegate_repository import DelegateRunStore


class RuntimeMemoryStore(Protocol):
    """Memory access for a run, keyed by scope kind + scope id."""

    async def load(self, scope_kind: str, scope_id: str | None) -> list[Message]: ...

    async def save(self, scope_kind: str, scope_id: str | None, messages: list[Message]) -> None: ...


class RuntimeMemoryAdapter:
    """Routes 'session' → SessionStore, 'delegate' → DelegateRunStore, 'none' → no-op.

    Unknown kinds, missing scope ids, and unwired stores (None) degrade to
    no-ops rather than raising: a mis-scoped run must never crash the agent
    loop. Messages are deep-copied at this boundary so callers can never
    share — or corrupt — a store's internal message objects.
    """

    def __init__(
        self,
        session_store: SessionStore | None,
        delegate_store: "DelegateRunStore | None",
    ) -> None:
        self._session_store = session_store
        self._delegate_store = delegate_store

    async def load(self, scope_kind: str, scope_id: str | None) -> list[Message]:
        if not scope_id:
            return []
        if scope_kind == "delegate":
            if self._delegate_store is None:
                return []
            messages = await self._delegate_store.load_messages(scope_id)
        elif scope_kind == "session":
            if self._session_store is None:
                return []
            messages = await self._session_store.load_messages(scope_id)
        else:
            return []
        return [message.model_copy(deep=True) for message in messages]

    async def save(self, scope_kind: str, scope_id: str | None, messages: list[Message]) -> None:
        if not scope_id:
            return
        if scope_kind == "delegate":
            if self._delegate_store is None:
                return
            await self._delegate_store.save_messages(
                scope_id, [message.model_copy(deep=True) for message in messages]
            )
        elif scope_kind == "session":
            if self._session_store is None:
                return
            await self._session_store.save_messages(
                scope_id, [message.model_copy(deep=True) for message in messages]
            )
