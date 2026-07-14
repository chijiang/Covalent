from __future__ import annotations

import asyncio
import os
import sys
import unittest

from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.backend import make_backend
from agent_framework.runtime.filesystem_backend import FileSystemBackend


class FileSystemBackendTests(unittest.TestCase):
    """Prove the ``FileSystemBackend`` seam without needing skill manifests or a DB."""

    def test_spawn_stream_runs_process_and_returns_piped_stdio(self) -> None:
        async def run() -> bytes:
            backend = FileSystemBackend()
            process = await backend.spawn_stream(
                [sys.executable, "-c", "print('hi from sandbox')"],
                cwd=None,
                env=dict(os.environ),
            )
            stdout = await process.stdout.read()
            await process.wait()
            return stdout

        stdout = asyncio.run(run())
        self.assertIn(b"hi from sandbox", stdout)

    def test_make_backend_filesystem(self) -> None:
        backend = make_backend(AppSettings(execution_backend_kind="filesystem"))
        self.assertIsInstance(backend, FileSystemBackend)

    def test_make_backend_docker_requires_skill_source_dirs_provider(self) -> None:
        # The Docker backend needs a skill-source-dirs provider so it can
        # bind-mount skill code into the session container; selecting docker
        # without one fails fast. (The backend itself is covered by
        # test_docker_backend.py.)
        with self.assertRaises(ValueError):
            make_backend(AppSettings(execution_backend_kind="docker"))

    def test_make_backend_kubernetes_raises_not_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            make_backend(AppSettings(execution_backend_kind="kubernetes"))


if __name__ == "__main__":
    unittest.main()
