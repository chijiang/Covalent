"""Routing tests for the runtime memory port (RuntimeMemoryAdapter).

The port decouples the runtime from concrete stores: memory operations are
``(scope_kind, scope_id)`` pairs routed by kind — ``"session"`` to the
SessionStore, ``"delegate"`` to the delegate run store, ``"none"`` to a no-op.
These tests pin the routing (and the cross-store isolation) with real
in-memory stores.
"""

from __future__ import annotations

import unittest

from covalent.core.types import Message
from covalent.infra.delegate_repository import InMemoryDelegateRunStore
from covalent.infra.memory import InMemorySessionStore

from covalent.runtime.memory_port import RuntimeMemoryAdapter


def _message(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


class MemoryPortTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(self) -> tuple[RuntimeMemoryAdapter, InMemorySessionStore, InMemoryDelegateRunStore]:
        session_store = InMemorySessionStore()
        delegate_store = InMemoryDelegateRunStore()
        return RuntimeMemoryAdapter(session_store, delegate_store), session_store, delegate_store

    async def test_session_scope_routes_to_session_store(self) -> None:
        adapter, session_store, delegate_store = self._adapter()

        await adapter.save("session", "s1", [_message("user", "hello")])

        loaded = await adapter.load("session", "s1")
        self.assertEqual([str(m.content) for m in loaded], ["hello"])
        # The delegate store must not have been touched.
        self.assertEqual(await delegate_store.count_messages("delegate-1"), 0)

    async def test_delegate_scope_routes_to_delegate_store(self) -> None:
        adapter, session_store, delegate_store = self._adapter()

        await adapter.save("delegate", "delegate-1", [_message("assistant", "done")])

        loaded = await adapter.load("delegate", "delegate-1")
        self.assertEqual([str(m.content) for m in loaded], ["done"])
        # The session store must not have been touched.
        self.assertEqual(await session_store.load_messages("s1"), [])

    async def test_none_scope_is_noop(self) -> None:
        adapter, session_store, delegate_store = self._adapter()
        await session_store.save_messages("s1", [_message("user", "prior")])
        await delegate_store.save_messages("delegate-1", [_message("assistant", "prior")])

        self.assertEqual(await adapter.load("none", None), [])
        await adapter.save("none", None, [_message("assistant", "must not persist")])

        self.assertEqual([str(m.content) for m in await session_store.load_messages("s1")], ["prior"])
        self.assertEqual(await delegate_store.count_messages("delegate-1"), 1)


if __name__ == "__main__":
    unittest.main()
