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

from covalent.runtime.filesystem_backend import FileSystemBackend
from covalent.skills.bundle import SkillBundle, slice_text_lines
from covalent.skills.meta_tools import _read_skill_instructions, _read_skill_resource, _run_skill_script
from covalent.skills.spec import ManifestSkillSpec, ScriptDeclaration, SkillSpec


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


class SkillInstructionReadTests(unittest.TestCase):
    def test_read_skill_instructions_returns_registered_body(self) -> None:
        skill = SkillSpec(
            name="test-skill",
            description="Test skill",
            instructions="abcdef",
            tools=[],
        )
        registry = SimpleNamespace(
            skills={"test-skill": skill},
            resolve_skill_name=lambda name: "test-skill",
            is_skill_enabled=lambda name: True,
        )

        result = json.loads(
            _read_skill_instructions(registry, {"skill": "test-skill", "max_chars": 3})
        )

        self.assertEqual(result["name"], "test-skill")
        self.assertEqual(result["instructions"], "abc")
        self.assertTrue(result["truncated"])


class SkillLineWindowTests(unittest.TestCase):
    """read_skill_instructions / read_skill_resource 的行窗口分页（对齐 deepagents 语义）。"""

    LINES = [f"line{i}" for i in range(1, 11)]
    TEXT = "\n".join(LINES)

    def test_slice_full_text_carries_pagination_metadata(self) -> None:
        window = slice_text_lines(self.TEXT, start_line=None, end_line=None)
        self.assertEqual(window["content"], self.TEXT)
        self.assertEqual((window["total_lines"], window["start_line"], window["end_line"]), (10, 1, 10))
        self.assertIsNone(window["next_offset"])
        self.assertFalse(window["truncated"])

    def test_slice_window_reports_next_offset(self) -> None:
        window = slice_text_lines(self.TEXT, start_line=3, end_line=5)
        self.assertEqual(window["content"], "line3\nline4\nline5")
        self.assertEqual(window["next_offset"], 6)

    def test_slice_byte_cap_mid_line_next_offset_repeats_cut_line(self) -> None:
        window = slice_text_lines(self.TEXT, start_line=None, end_line=None, max_bytes=25)
        self.assertTrue(window["truncated"])
        # 25 bytes 截到 "line1\n...\nline4\nl"：end_line 只数完整行，line5 可整行续读。
        self.assertEqual(window["end_line"], 4)
        self.assertEqual(window["next_offset"], 5)

    def test_slice_start_beyond_eof_raises(self) -> None:
        with self.assertRaises(ValueError):
            slice_text_lines(self.TEXT, start_line=99, end_line=None)

    def test_read_skill_instructions_line_window(self) -> None:
        skill = SkillSpec(
            name="test-skill",
            description="Test skill",
            instructions=self.TEXT,
            tools=[],
        )
        registry = SimpleNamespace(
            skills={"test-skill": skill},
            resolve_skill_name=lambda name: "test-skill",
            is_skill_enabled=lambda name: True,
        )

        result = json.loads(
            _read_skill_instructions(registry, {"skill": "test-skill", "start_line": 2, "end_line": 3})
        )

        self.assertEqual(result["instructions"], "line2\nline3")
        self.assertEqual(result["total_lines"], 10)
        self.assertEqual((result["start_line"], result["end_line"]), (2, 3))
        self.assertEqual(result["next_offset"], 4)

    def test_read_skill_resource_line_window_pages_through(self) -> None:
        with tempfile.TemporaryDirectory(prefix="af-skill-read-") as tmp:
            (Path(tmp) / "doc.md").write_text(self.TEXT + "\n", encoding="utf-8")
            spec = ManifestSkillSpec(
                name="test-skill",
                description="Test skill",
                source_dir=tmp,
                resource_files=["doc.md"],
            )
            bundle = SkillBundle(spec)

            page1 = bundle.read_resource("doc.md", max_bytes=10_000, start_line=1, end_line=4)
            self.assertEqual(page1["content"], "line1\nline2\nline3\nline4")
            self.assertEqual(page1["next_offset"], 5)

            page2 = bundle.read_resource("doc.md", max_bytes=10_000, start_line=page1["next_offset"])
            self.assertEqual(page2["content"], "line5\nline6\nline7\nline8\nline9\nline10")
            self.assertIsNone(page2["next_offset"])

    def test_read_skill_resource_rejects_line_range_on_binary(self) -> None:
        from covalent.skills.bundle import SkillBundleError

        with tempfile.TemporaryDirectory(prefix="af-skill-read-") as tmp:
            (Path(tmp) / "blob.bin").write_bytes(bytes(range(256)))
            spec = ManifestSkillSpec(
                name="test-skill",
                description="Test skill",
                source_dir=tmp,
                resource_files=["blob.bin"],
            )
            bundle = SkillBundle(spec)

            with self.assertRaises(SkillBundleError):
                bundle.read_resource("blob.bin", start_line=1)
            # 不带行参数的二元资源保持 base64 行为。
            result = bundle.read_resource("blob.bin")
            self.assertEqual(result["encoding"], "base64")


if __name__ == "__main__":
    unittest.main()
