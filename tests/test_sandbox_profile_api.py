"""HTTP-level tests for the sandbox profile administration routes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from covalent.infra.settings import AppSettings

from tests.test_sandbox_admin_api import (
    _admin_cookie,
    _build_app,
    _member_cookie,
)


class _FakeProfileService:
    """Stands in for the application SandboxProfileService at the HTTP layer."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict] = {
            "profile-python": {
                "id": "profile-python",
                "name": "Python 3.12",
                "workspace_id": None,
                "enabled": True,
                "is_default": True,
                "validation_status": "valid",
                "revision": 1,
                "image": "covalent-sandbox:dev",
                "runtime_capabilities": ["python", "shell"],
                "reference_counts": {"agents": 1, "instances": 2},
            },
            "profile-ws-private": {
                "id": "profile-ws-private",
                "name": "Other workspace",
                "workspace_id": "ws-other",
                "enabled": True,
                "is_default": False,
                "validation_status": "valid",
                "revision": 1,
                "image": "covalent-sandbox-node:dev",
                "runtime_capabilities": ["nodejs"],
                "reference_counts": {"agents": 0, "instances": 0},
            },
        }
        self.mutations: list[tuple[str, str]] = []

    async def profile_responses(self, workspace_id=None):
        out = []
        for profile in self.profiles.values():
            if profile["workspace_id"] is None or profile["workspace_id"] == workspace_id:
                out.append(dict(profile))
        return out

    async def profile_response(self, profile_id, workspace_id=None):
        from covalent.application.errors import NotFoundError

        profile = self.profiles.get(profile_id)
        if profile is None or (profile["workspace_id"] not in (None, workspace_id)):
            raise NotFoundError(f"sandbox profile '{profile_id}' not found")
        return dict(profile)

    async def create_profile(self, request, workspace_id=None):
        from covalent.application.errors import ConflictError

        existing = [p for p in self.profiles.values() if p["name"] == request.name]
        if existing:
            raise ConflictError("profile name already exists")
        profile = {
            "id": "profile-new",
            "name": request.name,
            "workspace_id": workspace_id,
            "enabled": False,
            "is_default": False,
            "validation_status": "pending",
            "revision": 1,
            "image": request.image,
            "runtime_capabilities": list(request.runtime_capabilities),
            "reference_counts": {"agents": 0, "instances": 0},
        }
        self.profiles[profile["id"]] = profile
        self.mutations.append(("create", request.name))
        return dict(profile)

    async def update_profile(self, profile_id, request, workspace_id=None):
        self.mutations.append(("update", profile_id))
        profile = self.profiles.get(profile_id)
        return dict(profile or {})

    async def validate_profile(self, profile_id, workspace_id=None):
        self.mutations.append(("validate", profile_id))
        return dict(self.profiles.get(profile_id, {}))

    async def enable_profile(self, profile_id, workspace_id=None):
        self.mutations.append(("enable", profile_id))
        return dict(self.profiles.get(profile_id, {}))

    async def disable_profile(self, profile_id, workspace_id=None):
        self.mutations.append(("disable", profile_id))
        return dict(self.profiles.get(profile_id, {}))

    async def delete_profile(self, profile_id, workspace_id=None):
        self.mutations.append(("delete", profile_id))
        return self.profiles.pop(profile_id, None) is not None


class SandboxProfileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
        self.profile_service = _FakeProfileService()
        self.app, self.client = _build_app(settings=self.settings)
        self.app.state.sandbox_profile_service = self.profile_service

    def _admin(self):
        return {"Cookie": _admin_cookie(self.settings)}

    def _member(self):
        return {"Cookie": _member_cookie(self.settings)}

    def test_list_profiles_member_sees_global_and_own_workspace(self) -> None:
        resp = self.client.get("/sandbox/profiles", headers=self._member())
        self.assertEqual(resp.status_code, 200)
        ids = {p["id"] for p in resp.json()}
        self.assertIn("profile-python", ids)  # global
        self.assertNotIn("profile-ws-private", ids)  # other workspace hidden

    def test_get_profile_visible(self) -> None:
        resp = self.client.get("/sandbox/profiles/profile-python", headers=self._member())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Python 3.12")
        self.assertEqual(resp.json()["reference_counts"]["instances"], 2)

    def test_get_profile_hides_other_workspace(self) -> None:
        resp = self.client.get("/sandbox/profiles/profile-ws-private", headers=self._member())
        self.assertEqual(resp.status_code, 404)

    def test_create_profile_requires_admin(self) -> None:
        body = {
            "name": "Node 22",
            "image": "covalent-sandbox-node:dev",
            "keepalive_command": ["tail", "-f", "/dev/null"],
            "runtime_capabilities": ["nodejs"],
            "memory_limit": "512m",
            "pids_limit": 256,
            "cpus": 1.0,
            "tmpfs_size": "128m",
        }
        member_resp = self.client.post("/sandbox/profiles", json=body, headers=self._member())
        self.assertEqual(member_resp.status_code, 403)
        admin_resp = self.client.post("/sandbox/profiles", json=body, headers=self._admin())
        self.assertEqual(admin_resp.status_code, 200)
        self.assertEqual(admin_resp.json()["name"], "Node 22")
        self.assertIn(("create", "Node 22"), self.profile_service.mutations)

    def test_disable_profile_requires_admin(self) -> None:
        member_resp = self.client.post(
            "/sandbox/profiles/profile-python/disable", headers=self._member()
        )
        self.assertEqual(member_resp.status_code, 403)
        admin_resp = self.client.post(
            "/sandbox/profiles/profile-python/disable", headers=self._admin()
        )
        self.assertEqual(admin_resp.status_code, 200)

    def test_delete_profile_requires_admin(self) -> None:
        resp = self.client.delete("/sandbox/profiles/profile-python", headers=self._member())
        self.assertEqual(resp.status_code, 403)
        resp = self.client.delete("/sandbox/profiles/profile-python", headers=self._admin())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "deleted")

    def test_validate_profile_requires_admin(self) -> None:
        resp = self.client.post("/sandbox/profiles/profile-python/validate", headers=self._member())
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/sandbox/profiles/profile-python/validate", headers=self._admin())
        self.assertEqual(resp.status_code, 200)

    def test_unauthenticated_returns_401(self) -> None:
        resp = self.client.get("/sandbox/profiles")
        self.assertIn(resp.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
