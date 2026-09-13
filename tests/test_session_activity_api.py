"""Tests for activity payload stripping in GET /sessions/{id} and the
on-demand detail endpoint GET /sessions/{id}/activity/{activity_id}."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, UTC
from types import SimpleNamespace

from starlette.testclient import TestClient

from covalent.api.app import create_app
from covalent.infra.memory import (
    ChatActivityItem,
    ChatSessionRecord,
    ChatTranscriptMessage,
    InMemorySessionStore,
)
from covalent.infra.settings import AppSettings


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
            messages=[ChatTranscriptMessage(id="um-1", role="user", content="hi")],
            activity=[
                ChatActivityItem(
                    id="act-1",
                    title="model_call",
                    payload={
                        "iteration": 1,
                        "model": "test-model",
                        "raw_request": {"messages": [{"role": "user", "content": "hi"}]},
                        "raw_response": {"text": "reply"},
                    },
                ),
                ChatActivityItem(
                    id="act-2",
                    title="tool_calls",
                    payload={"calls": [{"name": "search"}]},
                ),
            ],
        )
    ))


class SessionActivityStripTestCase(unittest.TestCase):
    def test_list_response_strips_raw_payloads_and_sets_flags(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.get("/sessions/sess-1", headers=headers)

        self.assertEqual(resp.status_code, 200)
        activity = resp.json()["activity"]
        model_call = next(item for item in activity if item["id"] == "act-1")
        self.assertEqual(model_call["payload"].keys(), {"iteration", "model"})
        self.assertTrue(model_call["has_raw_request"])
        self.assertTrue(model_call["has_raw_response"])
        tool_calls = next(item for item in activity if item["id"] == "act-2")
        self.assertEqual(tool_calls["payload"], {"calls": [{"name": "search"}]})
        self.assertFalse(tool_calls["has_raw_request"])
        self.assertFalse(tool_calls["has_raw_response"])

    def test_detail_endpoint_returns_full_payload(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.get("/sessions/sess-1/activity/act-1", headers=headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["id"], "act-1")
        self.assertEqual(body["title"], "model_call")
        self.assertIn("raw_request", body["payload"])
        self.assertIn("raw_response", body["payload"])

    def test_detail_unknown_activity_404(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.get("/sessions/sess-1/activity/nope", headers=headers)

        self.assertEqual(resp.status_code, 404)

    def test_detail_unknown_session_404(self) -> None:
        store = InMemorySessionStore()
        client = _build_app_with_store(store)
        headers = {"Cookie": _admin_cookie(client.app)}

        resp = client.get("/sessions/missing/activity/act-1", headers=headers)

        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
