"""Tests for PUT /sessions/{id}/transcript (edit-and-resend)."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, UTC
from types import SimpleNamespace

from starlette.testclient import TestClient

from covalent.api.app import create_app
from covalent.application.schemas import (
    ChatTranscriptMessageInput,
    TranscriptReplaceRequest,
)
from covalent.infra.memory import (
    ChatSessionRecord,
    ChatTranscriptMessage,
    InMemorySessionStore,
)
from covalent.infra.settings import AppSettings


class TranscriptReplaceRequestSchemaTestCase(unittest.TestCase):
    def test_truncate_mode(self) -> None:
        req = TranscriptReplaceRequest(truncate_before_message_id="um-2")
        self.assertEqual(req.truncate_before_message_id, "um-2")
        self.assertIsNone(req.messages)

    def test_explicit_replace_mode(self) -> None:
        msg = ChatTranscriptMessageInput(id="um-1", role="user", content="hi")
        req = TranscriptReplaceRequest(messages=[msg])
        self.assertEqual(req.messages, [msg])
        self.assertIsNone(req.truncate_before_message_id)

    def test_message_attachments_default_empty(self) -> None:
        msg = ChatTranscriptMessageInput(id="um-1", role="user", content="hi")
        self.assertEqual(msg.attachments, [])


# ---------------------------------------------------------------------------
# Route-level tests — build the app the same way test_agent_crud_api.py does
# (create_app() + swap app.state.* with fakes + an admin fake-DB so the resolved
# principal is an admin and the owner check passes under console_auth_mode=local
# with anonymous requests), but swap in a real InMemorySessionStore so messages
# persist. Sessions are seeded directly through the store.
# ---------------------------------------------------------------------------
class _FakeTransaction:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _FakeDbSession:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        if model.__name__ == "UserRow":
            return SimpleNamespace(
                id=key or "admin", email="admin@local", display_name="Local Admin",
                role="admin", status="active", avatar_url=None, username="admin",
                preferences_json={},
            )
        if model.__name__ == "AgentRow":
            return SimpleNamespace(
                name=key or "default", display_name="Default Agent",
                owner_user_id="admin", visibility="public", publication_status="approved",
            )
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalar(self, *a, **kw):
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalars(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return None

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


def _build_app_with_store(store: InMemorySessionStore):
    app = create_app()
    app.state.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
    app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
    app.state.registry = SimpleNamespace()
    app.state.runtime = SimpleNamespace()
    app.state.config_store = SimpleNamespace()
    app.state.execution_backend = SimpleNamespace(name="filesystem")
    app.state.skill_loader = SimpleNamespace()
    app.state.session_store = store
    return TestClient(app)


def _admin_cookie(app) -> str:
    """Signed console-session cookie for an admin principal (local auth mode).

    Under console_auth_mode=local the ConsoleAuthGuardMiddleware rejects
    anonymous requests with 401, so tests must present a valid signed session
    cookie (same proven pattern as tests/test_agent_crud_api.py).
    """
    from covalent.api._auth_helpers import _make_console_session_token
    from covalent.api._shared import ConsolePrincipalContext
    settings = app.state.settings
    principal = ConsolePrincipalContext(
        user_id="admin", email="admin@local", display_name="Local Admin",
        role="admin", workspace_id="11111111-1111-1111-1111-111111111111",
        workspace_name="Workspace 1", workspace_slug="ws-1", workspace_role="admin",
    )
    return f"{settings.console_session_cookie_name}={_make_console_session_token(settings, principal)}"


def _seed_session(store: InMemorySessionStore, session_id: str) -> None:
    """Seed [um-1 user, am-1 assistant, um-2 user, am-2 assistant]."""
    asyncio.run(store.save_session(
        ChatSessionRecord(
            id=session_id,
            title="t",
            title_source="auto",
            agent_name="default",
            owner_user_id=None,
            workspace_id=None,
            preview_text="",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            memory_messages=[],
            messages=[
                ChatTranscriptMessage(id="um-1", role="user", content="first"),
                ChatTranscriptMessage(id="am-1", role="assistant", content="reply 1"),
                ChatTranscriptMessage(id="um-2", role="user", content="second"),
                ChatTranscriptMessage(id="am-2", role="assistant", content="reply 2"),
            ],
            activity=[],
        )
    ))


class TranscriptReplaceRouteTestCase(unittest.TestCase):
    def test_truncate_drops_message_and_tail(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "um-2"}, headers=headers)

        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.json()["messages"]]
        self.assertEqual(ids, ["um-1", "am-1"])

    def test_truncate_unknown_message_id_404(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "nope"}, headers=headers)

        self.assertEqual(resp.status_code, 404)

    def test_explicit_replace_restores_tail(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript", json={
            "messages": [
                {"id": "um-1", "role": "user", "content": "first"},
                {"id": "am-1", "role": "assistant", "content": "reply 1"},
                {"id": "um-2", "role": "user", "content": "second"},
                {"id": "am-2", "role": "assistant", "content": "reply 2"},
            ],
        }, headers=headers)

        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.json()["messages"]]
        self.assertEqual(ids, ["um-1", "am-1", "um-2", "am-2"])

    def test_explicit_replace_rejects_empty(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript", json={"messages": []}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_neither_field_supplied_400(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript", json={}, headers=headers)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_session_404(self) -> None:
        store = InMemorySessionStore()
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/missing/transcript",
                          json={"truncate_before_message_id": "um-1"}, headers=headers)
        self.assertEqual(resp.status_code, 404)


class TranscriptReplaceStoreRoundTripTestCase(unittest.TestCase):
    def test_truncate_then_reload_reflects_truncation(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "um-2"}, headers=headers)
        self.assertEqual(resp.status_code, 200)

        # The store itself must reflect the truncation (this is what the stream
        # route will load on the next turn).
        reloaded = asyncio.run(store.get_session("sess-1"))
        self.assertIsNotNone(reloaded)
        ids = [m.id for m in reloaded.messages]
        self.assertEqual(ids, ["um-1", "am-1"])

    def test_replace_preserves_title_agent_and_memory(self) -> None:
        store = InMemorySessionStore()
        # Seed with a non-default title and a memory message so we can assert
        # they survive the replace.
        asyncio.run(store.save_session(
            ChatSessionRecord(
                id="sess-2", title="Important", title_source="manual",
                agent_name="researcher", owner_user_id=None, workspace_id=None,
                preview_text="old preview", created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                memory_messages=[],  # memory is loaded separately; just assert it stays empty
                messages=[
                    ChatTranscriptMessage(id="um-1", role="user", content="hello"),
                ],
                activity=[],
            )
        ))
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.put("/sessions/sess-2/transcript", json={
            "messages": [{"id": "um-1", "role": "user", "content": "hello edited"}],
        }, headers=headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "Important")
        self.assertEqual(body["title_source"], "manual")
        self.assertEqual(body["agent_name"], "researcher")
        self.assertEqual(body["messages"][0]["content"], "hello edited")
        # preview is recomputed from the new message list, not preserved verbatim
        self.assertIn("hello edited", body["preview_text"])


if __name__ == "__main__":
    unittest.main()
