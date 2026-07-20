"""Tests for backend.workspace() -> WorkspaceAccess (Phase 2).

The workspace file tools resolve their root through the backend; FS and Docker
both expose a host path (Docker via bind mount). These pin that contract.
"""

from __future__ import annotations

import tempfile
import unittest

from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.backend import HostPathWorkspace
from agent_framework.runtime.docker_backend import DockerBackend
from agent_framework.runtime.filesystem_backend import FileSystemBackend


class WorkspaceAccessTests(unittest.TestCase):
    def test_filesystem_workspace_returns_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(workspace_root_dir=tmp)
            backend = FileSystemBackend(settings)
            access = backend.workspace("sess-1")
            self.assertIsInstance(access, HostPathWorkspace)
            self.assertEqual(access.host_path, settings.session_workspace_dir("sess-1"))

    def test_filesystem_workspace_base_root_when_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(workspace_root_dir=tmp)
            backend = FileSystemBackend(settings)
            self.assertEqual(backend.workspace(None).host_path, settings.workspace_root())

    def test_filesystem_workspace_raises_without_settings(self) -> None:
        backend = FileSystemBackend()  # default path (e.g. SkillProcessManager) has no settings
        with self.assertRaises(RuntimeError):
            backend.workspace("sess-1")

    def test_docker_workspace_returns_session_dir(self) -> None:
        # workspace() reads only settings (no docker calls), so a dummy client is fine.
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(workspace_root_dir=tmp)
            backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [], docker_client=object())
            access = backend.workspace("sess-1")
            self.assertIsInstance(access, HostPathWorkspace)
            self.assertEqual(access.host_path, settings.session_workspace_dir("sess-1"))


if __name__ == "__main__":
    unittest.main()
