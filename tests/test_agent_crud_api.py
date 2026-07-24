"""HTTP-level tests for agent CRUD with allowed_outbound.

Uses ``create_app()`` + ``TestClient`` with a fake ``ConfigStore`` to verify
that ``PUT /config/agents`` with ``allowed_outbound`` round-trips correctly
through ``GET /agents`` and ``GET /agents/{name}``.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from starlette.testclient import TestClient

from agent_framework.api.app import create_app
from agent_framework.infra.settings import AppSettings
from agent_framework.registry.registry import FrameworkRegistry


# ---------------------------------------------------------------------------
# Fake ConfigStore — stores agent documents in memory.
# ---------------------------------------------------------------------------
class _FakeConfigStore:
    def __init__(self, docs: dict[str, list[dict]] | None = None):
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


# Default provider seed — needed by _apply_runtime_config → _build_agent_specs
_DEFAULT_PROVIDER = {
    "provider": "openai_compatible",
    "model": "gpt-4o-mini",
    "api_key": None,
    "base_url": "https://api.openai.com/v1",
    "timeout_seconds": 500.0,
    "extra": {},
}

# Minimal agent payload (seeded initially).
_AGENT_PAYLOAD: list[dict] = [{
    "name": "default",
    "description": "Default agent",
    "system_prompt": "You are a helpful assistant.",
    "provider": _DEFAULT_PROVIDER,
    "local_tools": ["get_current_time"],
    "allowed_outbound": [],
    "capabilities": ["chat", "react", "tool_calling", "streaming"],
    "max_iterations": 10,
}]


# ---------------------------------------------------------------------------
# Reuse the fake DB session from test_sandbox_admin_api.
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
        # WorkspaceRow / WorkspaceMemberRow / etc.
        return SimpleNamespace(
            id=key or "11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalar(self, *a, **kw):
        # Return a stub that covers all scalar queries the auth flow makes
        # (WorkspaceRow, WorkspaceMemberRow). UUID-like id to satisfy any FK.
        return SimpleNamespace(id="11111111-1111-1111-1111-111111111111",
                               name="Workspace 1", slug="ws-1", role="admin")

    async def scalars(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return None

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


def _build_app(*, config_store=None, settings=None):
    app = create_app()
    app.state.settings = settings or AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
    app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
    app.state.registry = FrameworkRegistry()
    app.state.runtime = SimpleNamespace()
    app.state.config_store = config_store or _FakeConfigStore({"agents": _AGENT_PAYLOAD, "providers": [_DEFAULT_PROVIDER]})
    app.state.execution_backend = SimpleNamespace(name="filesystem")
    app.state.session_store = SimpleNamespace()

    # Seed the initial agent so /agents/{name} works.
    from agent_framework.api.app import _resolve_default_provider, _build_agent_specs
    return app, TestClient(app)


async def _seed_registry(app) -> None:
    """Run build_agent_specs after creating the app (must be async)."""
    from agent_framework.api.app import _resolve_default_provider, _build_agent_specs
    from agent_framework.mcp.spec import McpServerConfig
    settings = app.state.settings
    config_store = app.state.config_store
    provider = await _resolve_default_provider(settings, config_store, [])
    agents = _build_agent_specs(await config_store.get_document("agents"), provider, [], settings)
    for agent in agents:
        app.state.registry.register_agent(agent)


def _admin_cookie(settings):
    from agent_framework.api.app import _make_console_session_token, ConsolePrincipalContext
    p = ConsolePrincipalContext(user_id="admin", email="admin@t", display_name="A", role="admin",
                                workspace_id="w", workspace_name="W", workspace_slug="w", workspace_role="admin")
    return f"{settings.console_session_cookie_name}={_make_console_session_token(settings, p)}"


class AgentCrudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.app, self.client = _build_app(
            config_store=_FakeConfigStore({"agents": _AGENT_PAYLOAD, "providers": [_DEFAULT_PROVIDER]}),
            settings=self.settings,
        )
        # Direct-register so GET /agents/{name} works.
        from agent_framework.core.agent import AgentSpec
        from agent_framework.model.base import ProviderConfig
        self.app.state.registry.register_agent(AgentSpec(
            name="default", description="Default agent",
            system_prompt="You are a helpful assistant.",
            provider=ProviderConfig(provider="openai_compatible", model="gpt-4o-mini"),
            local_tools=["get_current_time"],
            allowed_outbound=[],
        ))

    # ------------------------------------------------------------------
    # GET /agents
    # ------------------------------------------------------------------
    async def test_list_agents_returns_default(self) -> None:
        resp = self.client.get("/agents", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        names = [a["name"] for a in resp.json()]
        self.assertIn("default", names)

    # ------------------------------------------------------------------
    # GET /agents/{name} — includes allowed_outbound
    # ------------------------------------------------------------------
    async def test_get_agent_includes_allowed_outbound(self) -> None:
        resp = self.client.get("/agents/default", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["allowed_outbound"], [])

    # ------------------------------------------------------------------
    # PUT /config/agents — create, update, delete (round-trip)
    # ------------------------------------------------------------------
    async def test_agent_crud_allowed_outbound_survives(self) -> None:
        payload = list(_AGENT_PAYLOAD)
        payload[0]["allowed_outbound"] = ["api.example.com"]

        # PUT with allowed_outbound.
        resp = self.client.put(
            "/config/agents",
            json={"raw": json.dumps(payload, ensure_ascii=False)},
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)

        # GET should reflect the change.
        resp = self.client.get("/agents/default", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["allowed_outbound"], ["api.example.com"])

        # Update system_prompt.
        payload[0]["system_prompt"] = "Updated prompt"
        resp = self.client.put(
            "/config/agents",
            json={"raw": json.dumps(payload, ensure_ascii=False)},
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/agents/default", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["system_prompt"], "Updated prompt")
        # allowed_outbound should still be there after update.
        self.assertEqual(resp.json()["allowed_outbound"], ["api.example.com"])

        # Delete — PUT without the agent.
        resp = self.client.put(
            "/config/agents",
            json={"raw": "[]"},
            headers={"Cookie": _admin_cookie(self.settings)},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/agents/default", headers={"Cookie": _admin_cookie(self.settings)})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
