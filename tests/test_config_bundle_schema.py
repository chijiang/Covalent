"""Unit tests for the config bundle schema (no database needed)."""

from __future__ import annotations

import unittest
import zipfile

import yaml

from covalent.application.services.config_bundle_schema import (
    BUNDLE_KIND,
    SCHEMA_VERSION,
    ConfigBundle,
    validate_schema_version,
)
from covalent.application.services.config_bundle_service import (
    BUNDLE_CONFIG_NAME,
    read_bundle,
)


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
                "owner_email": "alice@example.com",
                "workspace_slug": "acme",
            }
        ],
        "mcp_servers": [
            {
                "name": "srv",
                "transport": "stdio",
                "command": "echo",
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
                "sandbox_profile_id": "profile-1",
                "mcp_servers": ["srv"],
                "mcp_tools": [{"server_name": "srv", "tool_name": "t1"}],
                "capabilities": ["chat", "react"],
                "owner_email": "alice@example.com",
                "workspace_slug": "acme",
                "visibility": "public",
                "publication_status": "approved",
            }
        ],
    }


class SchemaVersionTests(unittest.TestCase):
    def test_current_version_is_accepted(self) -> None:
        self.assertEqual(validate_schema_version(SCHEMA_VERSION), [])

    def test_older_than_minimum_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_schema_version(0)

    def test_newer_than_known_warns(self) -> None:
        warnings = validate_schema_version(SCHEMA_VERSION + 5)
        self.assertEqual(len(warnings), 1)
        self.assertIn("newer", warnings[0])


class ConfigBundleRoundTripTests(unittest.TestCase):
    def test_yaml_round_trip_preserves_content(self) -> None:
        payload = _sample_bundle_payload()
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        reparsed = yaml.safe_load(text)
        self.assertEqual(ConfigBundle.model_validate(reparsed), ConfigBundle.model_validate(payload))


class ReadBundleTests(unittest.TestCase):
    def _write_bundle(self, path, payload=None, extra_files=None) -> None:
        payload = payload if payload is not None else _sample_bundle_payload()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(BUNDLE_CONFIG_NAME, yaml.safe_dump(payload))
            for name, content in (extra_files or {}).items():
                archive.writestr(name, content)

    def test_read_bundle_returns_config_and_skill_entries(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            self._write_bundle(
                path,
                extra_files={"skills/uploaded/demo/SKILL.md": "---\nname: demo\n---\nbody"},
            )
            bundle, skill_arcnames, warnings = read_bundle(path)
            self.assertEqual(bundle.metadata.kind, BUNDLE_KIND)
            self.assertEqual(len(bundle.agents), 1)
            self.assertEqual(skill_arcnames, ["skills/uploaded/demo/SKILL.md"])
            self.assertEqual(warnings, [])

    def test_missing_config_yaml_is_rejected(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("other.txt", "nope")
            with self.assertRaisesRegex(ValueError, "missing"):
                read_bundle(path)

    def test_wrong_kind_is_rejected(self) -> None:
        import tempfile
        from pathlib import Path

        payload = _sample_bundle_payload()
        payload["metadata"]["kind"] = "something-else"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            self._write_bundle(path, payload=payload)
            with self.assertRaisesRegex(ValueError, "Not a covalent config bundle"):
                read_bundle(path)

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            read_bundle("/nonexistent/bundle.zip")


if __name__ == "__main__":
    unittest.main()
