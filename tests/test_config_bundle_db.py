"""Integration tests for config bundle export/import.

These run against a real PostgreSQL instance pointed at by ``TEST_DATABASE_URL``
and auto-skip when it is unset (same convention as ``test_repositories.py``).
They exercise the full export → import → re-export path including the
natural-key remapping of users/workspaces.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml
from sqlalchemy import text

from covalent.application.services.config_bundle_schema import (
    BUNDLE_KIND,
    SCHEMA_VERSION,
)
from covalent.application.services.config_bundle_service import (
    BUNDLE_CONFIG_NAME,
    export_bundle,
    import_bundle,
)
from covalent.infra.migrations import run_database_migrations

_BUNDLE_TABLES = (
    "agents, agent_capabilities, agent_skills, agent_delegates, "
    "agent_mcp_servers, agent_mcp_tools, mcp_servers, mcp_server_env_vars, "
    "providers, skill_sources, skill_states, sandbox_profiles, "
    "workspace_members, users, workspaces"
)


class _FakeBundleSettings:
    """Minimal settings surface used by export/import and SkillLoader."""

    def __init__(self, database_url: str, skills_root: Path) -> None:
        self.database_url = database_url
        self.skills_root_dir = str(skills_root)
        self.skills_directories: str | None = None

    def resolve_path(self, path: str | None) -> Path | None:
        return Path(path).expanduser() if path else None

    def managed_skills_root(self) -> Path:
        return Path(self.skills_root_dir).expanduser()

    def managed_skill_directory(self, category: str) -> Path:
        return self.managed_skills_root() / category

    def local_skill_directories(self) -> list[Path]:
        return [self.managed_skill_directory(c) for c in ("built_in", "uploaded", "authored")]


def _sample_bundle_payload() -> dict:
    return {
        "metadata": {
            "kind": BUNDLE_KIND,
            "schema_version": SCHEMA_VERSION,
            "exported_at": "2026-01-01T00:00:00Z",
            "generator": "test",
        },
        "workspaces": [{"legacy_id": "ws-1", "slug": "acme", "name": "Acme"}],
        "users": [
            {
                "legacy_id": "user-1",
                "email": "alice@example.com",
                "username": "alice",
                "display_name": "Alice",
                "password_hash": "hash",
                "role": "admin",
                "status": "active",
            }
        ],
        "workspace_members": [{"workspace": "acme", "user_email": "alice@example.com", "role": "admin"}],
        "sandbox_profiles": [
            {
                "id": "profile-1",
                "name": "p1",
                "image": "img:1",
                "keepalive_command": ["sleep", "1"],
                "memory_limit": "512m",
                "pids_limit": 64,
                "cpus": 1.0,
                "tmpfs_size": "64m",
                "workspace": "acme",
            }
        ],
        "providers": [
            {
                "name": "prov",
                "internal_name": "prov",
                "provider_type": "openai_compatible",
                "base_url": "https://api.example.com",
                "api_key": "sk-secret",
                "default_model": "m1",
                "is_default": False,
                "position": 0,
                "visibility": "public",
                "publication_status": "approved",
                "owner_email": "alice@example.com",
                "workspace_slug": "acme",
            }
        ],
        "mcp_servers": [
            {
                "name": "srv",
                "transport": "stdio",
                "command": "echo",
                "args": [],
                "url": None,
                "env": {"K": "V"},
                "internal_name": "srv",
                "visibility": "public",
                "publication_status": "approved",
            }
        ],
        "skill_sources": [],
        "skill_states": [{"skill_name": "demo", "enabled": False}],
        "agents": [
            {
                "name": "agent-a",
                "internal_name": "agent-a",
                "description": "d",
                "system_prompt": "sp",
                "provider": {
                    "provider": "prov",
                    "model": "m1",
                    "api_key": "sk-agent",
                    "base_url": "https://api.example.com",
                    "timeout_seconds": 10,
                    "extra": {},
                },
                "skills": ["demo"],
                "local_tools": [],
                "allowed_outbound": [],
                "sandbox_profile_id": "profile-1",
                "delegate_agents": [],
                "mcp_servers": ["srv"],
                "mcp_tools": [{"server_name": "srv", "tool_name": "t1"}],
                "capabilities": ["chat", "react"],
                "max_iterations": 5,
                "context_window": None,
                "metadata": {},
                "enabled": True,
                "visibility": "public",
                "publication_status": "approved",
                "owner_email": "alice@example.com",
                "workspace_slug": "acme",
            }
        ],
    }


def _write_bundle(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BUNDLE_CONFIG_NAME, yaml.safe_dump(payload))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class BundleImportExportTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        os.environ["AGENT_FRAMEWORK_DATABASE_URL"] = cls.database_url
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        cls._tmp = tempfile.TemporaryDirectory()
        cls.skills_root = Path(cls._tmp.name) / "skills"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    async def asyncSetUp(self) -> None:
        self.settings = _FakeBundleSettings(self.database_url, self.skills_root)
        self.bundle_path = Path(self._tmp.name) / "bundle.zip"
        _write_bundle(self.bundle_path, _sample_bundle_payload())
        await self._truncate()

    async def asyncTearDown(self) -> None:
        await self._truncate()

    async def _truncate(self) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self.database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"TRUNCATE {_BUNDLE_TABLES} RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    async def _fetch_one(self, sql: str):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self.database_url)
        try:
            async with engine.begin() as conn:
                return (await conn.execute(text(sql))).mappings().first()
        finally:
            await engine.dispose()

    async def _scalar(self, sql: str):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self.database_url)
        try:
            async with engine.begin() as conn:
                return (await conn.execute(text(sql))).scalar()
        finally:
            await engine.dispose()

    async def test_import_into_empty_db_inserts_everything(self) -> None:
        report = await import_bundle(self.settings, self.bundle_path)
        self.assertEqual(report.kinds["workspaces"].inserted, 1)
        self.assertEqual(report.kinds["users"].inserted, 1)
        self.assertEqual(report.kinds["providers"].inserted, 1)
        self.assertEqual(report.kinds["mcp_servers"].inserted, 1)
        self.assertEqual(report.kinds["agents"].inserted, 1)
        self.assertEqual(report.warnings, [])

        provider = await self._fetch_one("SELECT * FROM providers WHERE name = 'prov'")
        self.assertEqual(provider["api_key"], "sk-secret")
        self.assertEqual(provider["workspace_id"], "ws-1")
        self.assertEqual(provider["owner_user_id"], "user-1")

        agent = await self._fetch_one("SELECT * FROM agents WHERE name = 'agent-a'")
        self.assertEqual(agent["sandbox_profile_id"], "profile-1")
        self.assertEqual(agent["provider_api_key"], "sk-agent")
        self.assertEqual(agent["position"], 0)

        profile = await self._fetch_one("SELECT * FROM sandbox_profiles WHERE id = 'profile-1'")
        self.assertEqual(profile["workspace_id"], "ws-1")

        self.assertEqual(
            await self._scalar("SELECT count(*) FROM agent_skills WHERE agent_name = 'agent-a'"), 1
        )
        self.assertEqual(
            await self._scalar("SELECT count(*) FROM agent_mcp_tools WHERE tool_name = 't1'"), 1
        )
        self.assertEqual(
            await self._scalar(
                "SELECT value FROM mcp_server_env_vars WHERE server_name = 'srv' AND key = 'K'"
            ),
            "V",
        )
        self.assertEqual(
            await self._scalar("SELECT enabled FROM skill_states WHERE skill_name = 'demo'"), False
        )

    async def test_reimport_overwrite_is_idempotent(self) -> None:
        await import_bundle(self.settings, self.bundle_path)
        report = await import_bundle(self.settings, self.bundle_path, on_conflict="overwrite")
        self.assertEqual(report.kinds["agents"].updated, 1)
        self.assertEqual(report.kinds["agents"].inserted, 0)
        self.assertEqual(await self._scalar("SELECT count(*) FROM agents"), 1)
        self.assertEqual(await self._scalar("SELECT count(*) FROM agent_skills"), 1)

    async def test_reimport_skip_keeps_existing_rows(self) -> None:
        await import_bundle(self.settings, self.bundle_path)
        report = await import_bundle(self.settings, self.bundle_path, on_conflict="skip")
        self.assertEqual(report.kinds["agents"].skipped, 1)
        self.assertEqual(await self._scalar("SELECT count(*) FROM agents"), 1)

    async def test_owner_remapped_to_existing_user(self) -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self.database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, username, display_name, role, status) "
                        "VALUES ('existing-user', 'alice@example.com', 'alice', 'Alice', 'member', 'active')"
                    )
                )
        finally:
            await engine.dispose()

        report = await import_bundle(self.settings, self.bundle_path)
        self.assertEqual(report.kinds["users"].updated, 1)
        self.assertEqual(report.kinds["users"].inserted, 0)
        agent = await self._fetch_one("SELECT * FROM agents WHERE name = 'agent-a'")
        self.assertEqual(agent["owner_user_id"], "existing-user")

    async def test_missing_sandbox_profile_cleared_with_warning(self) -> None:
        payload = _sample_bundle_payload()
        payload["agents"][0]["sandbox_profile_id"] = "ghost-profile"
        _write_bundle(self.bundle_path, payload)

        report = await import_bundle(self.settings, self.bundle_path)
        agent = await self._fetch_one("SELECT * FROM agents WHERE name = 'agent-a'")
        self.assertIsNone(agent["sandbox_profile_id"])
        self.assertTrue(any("ghost-profile" in w for w in report.warnings))

    async def test_strict_aborts_on_missing_sandbox_profile(self) -> None:
        payload = _sample_bundle_payload()
        payload["agents"][0]["sandbox_profile_id"] = "ghost-profile"
        _write_bundle(self.bundle_path, payload)
        with self.assertRaisesRegex(ValueError, "ghost-profile"):
            await import_bundle(self.settings, self.bundle_path, strict=True)
        self.assertEqual(await self._scalar("SELECT count(*) FROM agents"), 0)

    async def test_dry_run_writes_nothing(self) -> None:
        report = await import_bundle(self.settings, self.bundle_path, dry_run=True)
        self.assertEqual(report.kinds["agents"].inserted, 1)
        self.assertTrue(report.dry_run)
        for table in ("agents", "providers", "users", "workspaces"):
            self.assertEqual(await self._scalar(f"SELECT count(*) FROM {table}"), 0)

    async def test_round_trip_export_import_export_is_stable(self) -> None:
        await import_bundle(self.settings, self.bundle_path)
        first = Path(self._tmp.name) / "first.zip"
        await export_bundle(self.settings, first, include_skills=False)

        report = await import_bundle(self.settings, first, on_conflict="overwrite")
        self.assertEqual(report.kinds["agents"].updated, 1)
        second = Path(self._tmp.name) / "second.zip"
        await export_bundle(self.settings, second, include_skills=False)

        def load(path: Path) -> dict:
            with zipfile.ZipFile(path) as archive:
                return yaml.safe_load(archive.read(BUNDLE_CONFIG_NAME))

        left, right = load(first), load(second)
        left.pop("metadata"), right.pop("metadata")
        for section, items in left.items():
            normalized_left = sorted(items or [], key=lambda i: str(sorted(i.items())))
            normalized_right = sorted(right.get(section) or [], key=lambda i: str(sorted(i.items())))
            self.assertEqual(normalized_left, normalized_right, f"section {section} diverged")


if __name__ == "__main__":
    unittest.main()
