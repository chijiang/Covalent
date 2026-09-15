"""Regression tests for admin user renames being reverted by stale sessions.

``_resolve_console_principal`` used to write the session token's login-time
``name`` claim back into the users table on every request, so a renamed user
whose browser still held the old cookie silently reverted the admin's rename
the next time they clicked anything. In local/session auth the DB row is the
source of truth; only external identity modes (dev headers, trusted_header,
jwt) may overwrite an existing user's display name.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from starlette.testclient import TestClient

from covalent.api._auth_helpers import _make_console_session_token
from covalent.api._shared import ConsolePrincipalContext
from covalent.api.app import create_app
from covalent.infra.db import UserRow, WorkspaceMemberRow, WorkspaceRow
from covalent.infra.settings import AppSettings


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeDbSession:
    """In-memory stand-in exposing the lookups the auth flow performs, backed
    by mutable rows so tests can assert on (non-)mutations."""

    def __init__(self, users: dict, workspaces: dict, members: dict):
        self.users = users
        self.workspaces = workspaces
        self.members = members
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        store = {
            UserRow.__name__: self.users,
            WorkspaceRow.__name__: self.workspaces,
            WorkspaceMemberRow.__name__: self.members,
        }.get(model.__name__)
        return store.get(key) if store else None

    async def scalar(self, *a, **kw):
        return None

    async def flush(self):
        pass


def _user(display_name: str):
    return SimpleNamespace(
        id="u-1",
        email="u-1@test",
        username="u-1",
        display_name=display_name,
        role="member",
        status="active",
        avatar_url=None,
        preferences_json={},
    )


def _workspace_and_member():
    workspace = SimpleNamespace(id="w-1", name="W", slug="w")
    member = SimpleNamespace(workspace_id="w-1", user_id="u-1", role="member")
    return workspace, member


def _build_app(session, *, settings: AppSettings):
    app = create_app()
    app.state.settings = settings
    app.state.db_manager = SimpleNamespace(session_factory=lambda: session)
    app.state.registry = SimpleNamespace()
    app.state.runtime = SimpleNamespace()
    app.state.config_store = SimpleNamespace()
    app.state.execution_backend = SimpleNamespace(name="filesystem")
    app.state.skill_loader = SimpleNamespace()
    app.state.session_store = SimpleNamespace()
    return app, TestClient(app)


def _session_cookie(settings: AppSettings, *, display_name: str) -> str:
    principal = ConsolePrincipalContext(
        user_id="u-1",
        email="u-1@test",
        display_name=display_name,
        avatar_url=None,
        preferences={},
        role="member",
        workspace_id="w-1",
        workspace_name="W",
        workspace_slug="w",
        workspace_role="member",
        username="u-1",
    )
    return f"{settings.console_session_cookie_name}={_make_console_session_token(settings, principal)}"


class StaleSessionRenameTests(unittest.TestCase):
    """Local (session-cookie) auth: the DB row must win over the cookie."""

    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.user = _user("New Name")
        workspace, member = _workspace_and_member()
        self.db = _FakeDbSession(
            users={"u-1": self.user},
            workspaces={"w-1": workspace},
            members={("w-1", "u-1"): member},
        )
        self.app, self.client = _build_app(self.db, settings=self.settings)

    def test_stale_cookie_does_not_revert_admin_rename(self) -> None:
        # Cookie was minted at login, before the admin renamed u-1.
        headers = {"Cookie": _session_cookie(self.settings, display_name="Old Name")}
        resp = self.client.get("/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["display_name"], "New Name")
        self.assertEqual(self.user.display_name, "New Name")

    def test_empty_db_name_is_still_filled_from_cookie(self) -> None:
        self.user.display_name = ""
        headers = {"Cookie": _session_cookie(self.settings, display_name="Old Name")}
        resp = self.client.get("/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.user.display_name, "Old Name")


class ExternalIdentityModeTests(unittest.TestCase):
    """trusted_header / jwt remain authoritative for existing users."""

    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="trusted_header", workspace_root_dir="/tmp")
        self.user = _user("DB Name")
        workspace, member = _workspace_and_member()
        self.db = _FakeDbSession(
            users={"u-1": self.user},
            workspaces={"w-1": workspace},
            members={("w-1", "u-1"): member},
        )
        self.app, self.client = _build_app(self.db, settings=self.settings)

    def test_header_name_overwrites_existing_user(self) -> None:
        headers = {
            "x-covalent-user-id": "u-1",
            "x-covalent-user-email": "u-1@test",
            "x-covalent-user-name": "IdP Name",
        }
        resp = self.client.get("/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["display_name"], "IdP Name")
        self.assertEqual(self.user.display_name, "IdP Name")


if __name__ == "__main__":
    unittest.main()
