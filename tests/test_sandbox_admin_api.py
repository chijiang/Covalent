"""HTTP-level tests for sandbox admin endpoints and session lifecycle.

Uses ``create_app()`` + ``TestClient`` with stubbed state. Covers the routes
that were added or modified during the execution-backend work: sandbox status,
sandbox session stop, and session DELETE (which now calls ``backend.stop``).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from agent_framework.api.app import create_app
from agent_framework.infra.settings import AppSettings
from agent_framework.registry.registry import FrameworkRegistry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _DummySessionStore:
    async def get_session(self, session_id):
        return None


def _build_app(*, sandbox_backend=None, session_store=None, settings=None):
    """Construct a test app with stubbed singletons, dev-mode auth."""
    app = create_app()
    app.state.settings = settings or AppSettings(console_auth_mode="dev", workspace_root_dir="/tmp")
    # Simple db_manager stub: session_factory returns a no-op async context manager.
    app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
    app.state.registry = FrameworkRegistry()
    app.state.runtime = SimpleNamespace()
    if sandbox_backend is not None:
        app.state.execution_backend = sandbox_backend
    if session_store is not None:
        app.state.session_store = session_store
    elif not hasattr(app.state, "session_store"):
        app.state.session_store = _DummySessionStore()
    return app, TestClient(app)


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeDbSession:
    """A minimal fake DB session — stubs the handful of methods the auth /
    sandbox-routes call, plus a __getattr__ fallback for anything else."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        if model.__name__ == "UserRow":
            role = "admin" if key and "admin" in str(key) else "member"
            return SimpleNamespace(
                id=key or "anon",
                email=f"{key}@test",
                display_name=str(key),
                role=role,
                status="active",
                avatar_url=None,
                username=str(key or "user"),
                preferences_json={},
                workspace_id="ws-1",
                workspace_name="Workspace 1",
                workspace_slug="ws-1",
                workspace_role=role,
            )
        return None

    async def scalar(self, *args, **kwargs):
        return None

    async def scalars(self, *args, **kwargs):
        return []

    async def execute(self, *args, **kwargs):
        return None

    def add(self, instance):
        return None

    def __getattr__(self, name):
        # Catch-all: return a no-op for any method we didn't explicitly stub
        # (e.g. refresh, flush).
        async def _noop(*args, **kwargs):
            return None
        return _noop


def _admin_cookie(settings: AppSettings) -> str:
    """Build a console session cookie for an admin identity."""
    from agent_framework.api.app import _make_console_session_token, ConsolePrincipalContext

    principal = ConsolePrincipalContext(
        user_id="admin-1",
        email="admin@test",
        display_name="Admin",
        role="admin",
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        workspace_slug="ws-1",
        workspace_role="admin",
    )
    token = _make_console_session_token(settings, principal)
    cookie_name = settings.console_session_cookie_name
    return f"{cookie_name}={token}"


def _member_cookie(settings: AppSettings) -> str:
    """Build a console session cookie for a non-admin identity."""
    from agent_framework.api.app import _make_console_session_token, ConsolePrincipalContext

    principal = ConsolePrincipalContext(
        user_id="member-1",
        email="member@test",
        display_name="Member",
        role="member",
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        workspace_slug="ws-1",
        workspace_role="member",
    )
    token = _make_console_session_token(settings, principal)
    cookie_name = settings.console_session_cookie_name
    return f"{cookie_name}={token}"


# ---------------------------------------------------------------------------
# Fake sandbox backend
# ---------------------------------------------------------------------------
class _FakeSandboxBackend:
    name = "docker"

    def __init__(self) -> None:
        self.stopped: list[str] = []
        self._snapshot = {
            "backend": "docker",
            "supported": True,
            "live": 2,
            "metrics": {"containers_started": 5, "containers_stopped": 3},
            "config": {"image": "covalent-sandbox:dev", "network": "none"},
            "sessions": [
                {"session_id": "s1", "agent_name": "agent-1", "status": "running",
                 "started_at": 1700000000.0, "network_mode": "none", "allowed_outbound": []},
                {"session_id": "s2", "agent_name": "agent-2", "status": "running",
                 "started_at": 1700000001.0, "network_mode": "bridge", "allowed_outbound": ["api.example.com"]},
            ],
        }

    async def sandbox_snapshot(self):
        return self._snapshot

    async def stop(self, session_id: str) -> None:
        self.stopped.append(session_id)

    async def startup_sweep(self):
        pass


class _FakeSessionStore:
    """Minimal session store for DELETE handler tests."""
    def __init__(self, sessions: dict | None = None):
        self._sessions = dict(sessions or {})
        self.deleted: list[str] = []
        self.renamed: list[tuple[str, str]] = []

    async def list_sessions(self, **filters):
        owner = filters.get("owner_user_id")
        results = list(self._sessions.values())
        if owner:
            results = [r for r in results if getattr(r, "owner_user_id", None) == owner]
        return results

    async def get_session(self, session_id):
        return self._sessions.get(session_id)

    async def update_title(self, session_id, title, title_source="manual"):
        self.renamed.append((session_id, title))
        record = self._sessions.get(session_id)
        if record:
            record.title = title
        return record

    async def delete_session(self, session_id) -> bool:
        self.deleted.append(session_id)
        return self._sessions.pop(session_id, None) is not None

    async def save_session(self, record):
        self._sessions[record.id] = record


# ---------------------------------------------------------------------------
# Sandbox admin tests
# ---------------------------------------------------------------------------
class SandboxAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.backend = _FakeSandboxBackend()
        self.app, self.client = _build_app(
            sandbox_backend=self.backend,
            settings=self.settings,
        )

    def test_status_admin_returns_snapshot(self) -> None:
        """Admin GET /sandbox/status → 200 with snapshot data."""
        resp = self.client.get(
            "/sandbox/status",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["supported"])
        self.assertEqual(data["live"], 2)
        self.assertEqual(len(data["sessions"]), 2)

    def test_status_member_returns_403(self) -> None:
        """Non-admin GET /sandbox/status → 403."""
        resp = self.client.get(
            "/sandbox/status",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 403)

    def test_status_no_session_returns_401(self) -> None:
        """Unauthenticated GET /sandbox/status → 401 (middleware gate)."""
        resp = self.client.get("/sandbox/status")
        self.assertIn(resp.status_code, (401, 403))

    def test_stop_session_admin_returns_ok(self) -> None:
        """Admin DELETE /sandbox/sessions/s1 → 200, backend.stop called."""
        resp = self.client.delete(
            "/sandbox/sessions/s1",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "stopped", "session_id": "s1"})
        self.assertIn("s1", self.backend.stopped)

    def test_stop_session_member_returns_403(self) -> None:
        """Non-admin DELETE /sandbox/sessions/s1 → 403."""
        resp = self.client.delete(
            "/sandbox/sessions/s1",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Session DELETE (with backend.stop) — the handler modified in Phase 1b
# ---------------------------------------------------------------------------
class SessionDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.backend = _FakeSandboxBackend()
        self.session_store = _FakeSessionStore({"sess-1": _SessionRecord("sess-1", user_id="member-1")})
        self.app, self.client = _build_app(
            sandbox_backend=self.backend,
            session_store=self.session_store,
            settings=self.settings,
        )

    def test_delete_session_calls_backend_stop(self) -> None:
        """DELETE /sessions/sess-1 → calls backend.stop(sess-1)."""
        resp = self.client.delete(
            "/sessions/sess-1",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sess-1", self.backend.stopped,
                      "delete_session should call backend.stop")

    def test_delete_nonexistent_session_returns_404(self) -> None:
        """DELETE /sessions/no-such → 404."""
        resp = self.client.delete(
            "/sessions/no-such",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 404)


class SessionListAndRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.backend = _FakeSandboxBackend()
        self.session_store = _FakeSessionStore({
            "s-1": _SessionRecord("s-1", user_id="member-1", title="Alpha"),
            "s-2": _SessionRecord("s-2", user_id="admin-1", title="Beta"),
        })
        self.app, self.client = _build_app(
            sandbox_backend=self.backend,
            session_store=self.session_store,
            settings=self.settings,
        )

    def test_list_sessions_admin_sees_all(self) -> None:
        resp = self.client.get(
            "/sessions",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreaterEqual(len(data), 1)
        titles = {r["title"] for r in data}
        self.assertIn("Alpha", titles)

    def test_list_sessions_member_sees_own_only(self) -> None:
        resp = self.client.get(
            "/sessions",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        titles = {r["title"] for r in data}
        self.assertIn("Alpha", titles)
        self.assertNotIn("Beta", titles)

    def test_get_session_returns_record(self) -> None:
        resp = self.client.get(
            "/sessions/s-1",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Alpha")

    def test_get_session_404_when_missing(self) -> None:
        resp = self.client.get(
            "/sessions/no-such",
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 404)

    def test_rename_session_updates_title(self) -> None:
        resp = self.client.patch(
            "/sessions/s-1",
            json={"title": "Renamed"},
            headers={"Cookie": _member_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Renamed")

class _SessionRecord:
    """A duck-type session record compatible with to_chat_session_summary_response."""
    def __init__(self, session_id: str, *, user_id: str = "member-1",
                 workspace_id: str = "ws-1", owner_user_id: str | None = None,
                 title: str = "Test Session"):
        self.id = session_id
        self.owner_user_id = owner_user_id or user_id
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.title = title
        self.created_at = "2024-01-01T00:00:00Z"
        self.updated_at = "2024-01-01T00:00:00Z"
        self.memory_messages = []
        self.memory_messages_json = []
        self.messages = []  # ChatSessionResponse needs this
        self.activity = []  # ChatSessionResponse needs this

    def model_dump(self, **kw):
        return {"id": self.id, "owner_user_id": self.owner_user_id,
                "workspace_id": self.workspace_id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "messages": [], "activity": []}


if __name__ == "__main__":
    unittest.main()
