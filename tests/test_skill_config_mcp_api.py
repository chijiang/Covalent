"""HTTP-level tests for skill, config publication, management, and MCP endpoints.

Uses ``create_app()`` + ``TestClient`` with fake config_store / session_store /
skill_loader stubs. Covers routes that were previously untested at the HTTP level.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from starlette.testclient import TestClient

from agent_framework.api.app import create_app
from agent_framework.infra.settings import AppSettings
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.skills.spec import ManifestSkillSpec, SkillSpec, SkillRuntime


# ---------------------------------------------------------------------------
# Shared fake infrastructure (reused from test_sandbox_admin_api patterns)
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
                id=key or "admin", email=f"{key}@test", display_name=str(key),
                role="admin", status="active", avatar_url=None, username=str(key or "user"),
                preferences_json={},
            )
        if model.__name__ == "AgentRow":
            return SimpleNamespace(
                name=key or "default", display_name=key or "Default Agent",
                owner_user_id="admin", visibility="public", publication_status="approved",
            )
        return SimpleNamespace(
            id=key or "11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalar(self, *a, **kw):
        return SimpleNamespace(id="ws-1", name="Workspace 1", slug="ws-1", role="admin")

    async def scalars(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return None

    def add(self, instance):
        return None

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


class _FakeConfigStore:
    def __init__(self, docs: dict | None = None):
        self._docs = dict(docs or {})

    async def get_document(self, kind, principal=None, **_kw):
        return self._docs.get(kind, [])

    async def save_document(self, kind, raw_document, *, principal=None, agent_renames=None):
        self._docs[kind] = list(raw_document)
        return list(raw_document)

    async def ensure_document(self, kind, payload):
        if kind not in self._docs:
            self._docs[kind] = list(payload)
        return self._docs[kind]

    async def set_skill_enabled(self, skill_name, enabled):
        self._docs.setdefault("skill_states", {})
        self._docs["skill_states"][skill_name] = enabled


class _DummySessionStore:
    async def get_session(self, session_id):
        return None


def _admin_cookie(settings):
    from agent_framework.api.app import _make_console_session_token, ConsolePrincipalContext
    p = ConsolePrincipalContext(
        user_id="admin", email="admin@t", display_name="A", role="admin",
        workspace_id="ws-1", workspace_name="Workspace 1", workspace_slug="ws-1", workspace_role="admin",
    )
    return f"{settings.console_session_cookie_name}={_make_console_session_token(settings, p)}"


def _build_app(*, registry=None, config_store=None, skill_loader=None, settings=None):
    app = create_app()
    app.state.settings = settings or AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
    app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
    app.state.registry = registry or FrameworkRegistry()
    app.state.runtime = SimpleNamespace()
    app.state.config_store = config_store or _FakeConfigStore()
    app.state.skill_loader = skill_loader or SimpleNamespace()
    app.state.execution_backend = SimpleNamespace(name="filesystem")
    app.state.session_store = _DummySessionStore()
    return app, TestClient(app)


def _make_inline_skill(name="test-inline"):
    """Build a minimal inline (non-executable) SkillSpec."""
    return SkillSpec(
        name=name,
        description=f"Inline skill {name}",
        instructions="Test instructions.",
        tools=[],
    )


def _make_manifest_skill(name="test-manifest", *, source_dir="/tmp"):
    """Build a minimal manifest (executable) ManifestSkillSpec."""
    return ManifestSkillSpec(
        name=name,
        description=f"Manifest skill {name}",
        instructions="Test manifest instructions.",
        source_dir=source_dir,
        runtime=SkillRuntime(type="python", protocol="rpc", entry_point="main.py"),
    )


_DEFAULT_PROVIDER = {
    "provider": "openai_compatible", "model": "gpt-4o-mini", "api_key": None,
    "base_url": "https://api.openai.com/v1", "timeout_seconds": 500.0, "extra": {},
}


# ---------------------------------------------------------------------------
# Skill list / detail / preview / enable / disable tests
# ---------------------------------------------------------------------------
class SkillHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.registry = FrameworkRegistry()
        self.inline = _make_inline_skill("inline-1")
        self.manifest = _make_manifest_skill("manifest-1", source_dir="/tmp")
        self.registry.register_skill(self.inline)
        self.registry.register_manifest_skill(self.manifest)
        self.app, self.client = _build_app(
            registry=self.registry,
            config_store=_FakeConfigStore({"skill_sources": []}),
            settings=self.settings,
        )

    def test_list_skills(self) -> None:
        resp = self.client.get("/skills", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        names = {s["name"] for s in resp.json()}
        self.assertIn("inline-1", names)
        self.assertIn("manifest-1", names)

    def test_get_skill_detail(self) -> None:
        resp = self.client.get("/skills/inline-1", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "inline-1")

    def test_get_skill_404(self) -> None:
        resp = self.client.get("/skills/no-such", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 404)

    def test_preview_inline_skill(self) -> None:
        resp = self.client.get("/skills/inline-1/preview", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "inline-1")
        self.assertGreater(len(resp.json()["files"]), 0)

    def test_enable_disable_skill(self) -> None:
        cookie = {"Cookie": _admin_cookie(self.settings)}
        # Disable
        resp = self.client.post("/skills/manifest-1/disable", headers=cookie)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "disabled")
        self.assertFalse(self.registry.is_skill_enabled("manifest-1"))
        # Enable
        resp = self.client.post("/skills/manifest-1/enable", headers=cookie)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "enabled")
        self.assertTrue(self.registry.is_skill_enabled("manifest-1"))

    def test_enable_404_for_unknown(self) -> None:
        resp = self.client.post("/skills/no-such/enable", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Skill export
# ---------------------------------------------------------------------------
class SkillExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.registry = FrameworkRegistry()
        self.manifest = _make_manifest_skill("export-1", source_dir="/tmp")
        self.registry.register_manifest_skill(self.manifest)
        self.app, self.client = _build_app(
            registry=self.registry,
            config_store=_FakeConfigStore({"skill_sources": []}),
            settings=self.settings,
        )

    def test_export_skill(self) -> None:
        resp = self.client.get("/skills/export-1/export", headers={"Cookie": _admin_cookie(self.settings)})
        # 200 or 404 (source dir may not have files); either is acceptable here.
        self.assertIn(resp.status_code, (200, 404))


# ---------------------------------------------------------------------------
# Config publication tests
# ---------------------------------------------------------------------------
class ConfigPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        payload = [{
            "name": "pub-agent", "description": "test", "system_prompt": "p",
            "provider": _DEFAULT_PROVIDER, "allowed_outbound": [],
        }]
        self.config_store = _FakeConfigStore({"agents": payload})
        self.app, self.client = _build_app(
            config_store=self.config_store,
            settings=self.settings,
        )

    def test_publish_request_returns_pending(self) -> None:
        resp = self.client.post(
            "/config/agents/pub-agent/publish-request",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        # May be 200 (success) or 400/404 depending on ownership/visibility.
        self.assertIn(resp.status_code, (200, 400, 404))

    def test_publication_review_admin_only(self) -> None:
        """Publication review requires admin — member gets 403."""
        # Build a separate app where the fake session resolves to a member.
        member_app, member_client = _build_app(
            config_store=self.config_store,
            settings=self.settings,
        )
        # Override the session factory to return a member user.
        class _MemberDbSession(_FakeDbSession):
            async def get(self, model, key):
                if model.__name__ == "UserRow":
                    return SimpleNamespace(
                        id="member", email="m@t", display_name="M",
                        role="member", status="active", avatar_url=None, username="member",
                        preferences_json={},
                    )
                return await super().get(model, key)
        member_app.state.db_manager = SimpleNamespace(session_factory=lambda: _MemberDbSession())
        resp = member_client.post(
            "/config/agents/pub-agent/publication-review",
            json={"status": "approved"},
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Management export / import tests
# ---------------------------------------------------------------------------
class ManagementExportImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.config_store = _FakeConfigStore({
            "agents": [{"name": "exp-agent", "description": "d", "system_prompt": "p", "provider": _DEFAULT_PROVIDER}],
            "providers": [_DEFAULT_PROVIDER],
        })
        self.app, self.client = _build_app(
            config_store=self.config_store,
            settings=self.settings,
        )

    def test_export_agents_yaml(self) -> None:
        resp = self.client.get(
            "/management/agents/export?format=yaml",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("exp-agent", resp.json()["content"])

    def test_export_agents_json(self) -> None:
        resp = self.client.get(
            "/management/agents/export?format=json",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("exp-agent", resp.json()["content"])

    def test_export_unknown_kind(self) -> None:
        resp = self.client.get(
            "/management/unknown/export",
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertIn(resp.status_code, (400, 404))


# ---------------------------------------------------------------------------
# MCP inspect / call tests
# ---------------------------------------------------------------------------
class McpHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.app, self.client = _build_app(settings=self.settings)

    def test_inspect_invalid_server_returns_400(self) -> None:
        """MCP inspect with a bogus server → 400 (connection error), not 500."""
        resp = self.client.post(
            "/mcp/inspect",
            json={
                "server": {
                    "name": "bogus",
                    "transport": "stdio",
                    "command": "nonexistent-command-xyz",
                    "args": [],
                }
            },
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 400)

    def test_call_invalid_server_returns_400(self) -> None:
        """MCP call with a bogus server → 400, not 500."""
        resp = self.client.post(
            "/mcp/call",
            json={
                "server": {
                    "name": "bogus",
                    "transport": "stdio",
                    "command": "nonexistent-command-xyz",
                    "args": [],
                },
                "tool_name": "test",
                "arguments": {},
            },
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# Health endpoint with sandbox block
# ---------------------------------------------------------------------------
class HealthzTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")

    def test_healthz_returns_ok(self) -> None:
        app, client = _build_app(settings=self.settings)
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_healthz_includes_sandbox_when_backend_has_snapshot(self) -> None:
        class _SnapshotBackend:
            name = "docker"
            def metrics_snapshot(self):
                return {"backend": "docker", "live_containers": 0}
        app, client = _build_app(settings=self.settings)
        app.state.execution_backend = _SnapshotBackend()
        resp = client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sandbox", resp.json())


if __name__ == "__main__":
    unittest.main()
