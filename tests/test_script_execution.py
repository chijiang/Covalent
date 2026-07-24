"""Tests for ``run_skill_script`` — ad-hoc skill script execution.

Uses ``FileSystemBackend`` (no Docker) to run real bash scripts via the
``_run_skill_script`` handler. Validates: basic execution, exit codes,
timeouts, argument passing, stdin piping.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_framework.runtime.filesystem_backend import FileSystemBackend
from agent_framework.skills.meta_tools import _run_skill_script
from agent_framework.skills.spec import ManifestSkillSpec, ScriptDeclaration


class _DummySettings:
    session_workspace_enabled = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace_root(self) -> Path:
        return self._root

    def session_workspace_dir(self, session_id: str) -> Path:
        return self._root / session_id


class ScriptExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-script-test-")
        self.tmpdir = Path(self._tmp.name)
        self.settings = _DummySettings(self.tmpdir)
        self.backend = FileSystemBackend()
        self.context = SimpleNamespace(session_id="s1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register_skill(self, scripts: list[ScriptDeclaration]) -> ManifestSkillSpec:
        spec = ManifestSkillSpec(
            name="test-skill",
            description="Test skill",
            source_dir=str(self.tmpdir),
            scripts=scripts,
        )
        # Register on a fake registry-like namespace.
        return spec

    async def test_script_basic_execution(self) -> None:
        """Script runs, stdout captured, exit_code=0."""
        (self.tmpdir / "hello.sh").write_text('#!/bin/sh\necho "hello from script"\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="hello", path="hello.sh", runtime="bash")])

        # Build a minimal fake registry.
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        result = json.loads(await _run_skill_script(
            registry, {"skill": "test-skill", "name": "hello"}, self.context, self.settings, self.backend,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello from script", result["stdout"])

    async def test_script_exit_code_nonzero(self) -> None:
        """Script exits non-zero → ok=False."""
        (self.tmpdir / "fail.sh").write_text('#!/bin/sh\necho "error output" >&2; exit 1\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="fail", path="fail.sh", runtime="bash")])
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        result = json.loads(await _run_skill_script(
            registry, {"skill": "test-skill", "name": "fail"}, self.context, self.settings, self.backend,
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("error output", result["stderr"])

    async def test_script_positional_args(self) -> None:
        """Positional args are passed to the script."""
        (self.tmpdir / "args.sh").write_text('#!/bin/sh\necho "got: $1 $2"\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="args", path="args.sh", runtime="bash")])
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        result = json.loads(await _run_skill_script(
            registry,
            {"skill": "test-skill", "name": "args", "positional_args": ["alpha", "beta"]},
            self.context, self.settings, self.backend,
        ))
        self.assertTrue(result["ok"])
        self.assertIn("got: alpha beta", result["stdout"])

    async def test_script_timeout(self) -> None:
        """Script that sleeps → RuntimeError after timeout."""
        (self.tmpdir / "slow.sh").write_text('#!/bin/sh\nsleep 10\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="slow", path="slow.sh", runtime="bash", timeout_seconds=60.0)])
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        with self.assertRaises(RuntimeError, msg="should time out"):
            await _run_skill_script(
                registry,
                {"skill": "test-skill", "name": "slow", "timeout_seconds": 1.0},
                self.context, self.settings, self.backend,
            )

    async def test_script_stdin(self) -> None:
        """stdin_data is piped to the script."""
        (self.tmpdir / "stdin.sh").write_text('#!/bin/sh\nread line; echo "got: $line"\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="stdin", path="stdin.sh", runtime="bash")])
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        result = json.loads(await _run_skill_script(
            registry,
            {"skill": "test-skill", "name": "stdin", "stdin_data": "piped-input"},
            self.context, self.settings, self.backend,
        ))
        self.assertTrue(result["ok"])
        self.assertIn("got: piped-input", result["stdout"])

    async def test_script_execution_backend_marker(self) -> None:
        """The result includes execution_backend = 'filesystem'."""
        (self.tmpdir / "mark.sh").write_text('#!/bin/sh\necho "ok"\n', encoding="utf-8")
        spec = self._register_skill([ScriptDeclaration(name="mark", path="mark.sh", runtime="bash")])
        registry = SimpleNamespace(
            manifest_skills={"test-skill": spec},
            resolve_skill_name=lambda name, **kw: "test-skill",
            is_skill_enabled=lambda name: True,
        )
        result = json.loads(await _run_skill_script(
            registry, {"skill": "test-skill", "name": "mark"}, self.context, self.settings, self.backend,
        ))
        self.assertEqual(result["execution_backend"], "filesystem")


if __name__ == "__main__":
    unittest.main()
