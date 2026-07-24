from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from agent_framework.core.workspace_tools import (
    _copy_workspace_entry,
    _edit_workspace_file,
    _move_workspace_entry,
    _publish_downloadable_file,
    _read_workspace_file,
    _search_workspace_files,
    _unzip_workspace_archive,
    _zip_workspace_entries,
)


class DummySettings:
    session_workspace_enabled = False

    def __init__(self, root: Path) -> None:
        self._root = root

    def workspace_root(self) -> Path:
        return self._root

    def session_workspace_dir(self, session_id: str) -> Path:
        return self._root / session_id


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.settings = DummySettings(self.root)
        self.context = SimpleNamespace(session_id=None)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_read_workspace_file_supports_full_line_and_byte_ranges(self) -> None:
        (self.root / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        truncated = json.loads(
            _read_workspace_file(self.settings, self.context, {"path": "notes.txt", "max_bytes": 5})
        )
        self.assertEqual(truncated["content"], "alpha")
        self.assertTrue(truncated["truncated"])

        full = json.loads(_read_workspace_file(self.settings, self.context, {"path": "notes.txt", "full": True}))
        self.assertEqual(full["content"], "alpha\nbeta\ngamma\n")
        self.assertFalse(full["truncated"])

        lines = json.loads(
            _read_workspace_file(
                self.settings,
                self.context,
                {"path": "notes.txt", "start_line": 2, "end_line": 3, "include_line_numbers": True},
            )
        )
        self.assertEqual(lines["content"], "2: beta\n3: gamma\n")
        self.assertEqual(lines["start_line"], 2)
        self.assertEqual(lines["end_line"], 3)

        byte_range = json.loads(
            _read_workspace_file(self.settings, self.context, {"path": "notes.txt", "offset": 6, "length": 4})
        )
        self.assertEqual(byte_range["content"], "beta")
        self.assertEqual(byte_range["offset"], 6)

    def test_search_workspace_files_finds_text_with_glob_and_context(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("alpha\nTarget line\nomega\n", encoding="utf-8")
        (self.root / "src" / "app.txt").write_text("Target ignored\n", encoding="utf-8")
        (self.root / ".hidden.py").write_text("Target hidden\n", encoding="utf-8")

        result = json.loads(
            _search_workspace_files(
                self.settings,
                self.context,
                {
                    "path": ".",
                    "query": "target",
                    "glob": "*.py",
                    "case_sensitive": False,
                    "context_lines": 1,
                },
            )
        )

        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["path"], "src/app.py")
        self.assertEqual(result["matches"][0]["line_number"], 2)
        self.assertEqual(
            result["matches"][0]["context"],
            [
                {"line_number": 1, "text": "alpha"},
                {"line_number": 2, "text": "Target line"},
                {"line_number": 3, "text": "omega"},
            ],
        )

    def test_edit_workspace_file_replaces_exact_text_and_ranges(self) -> None:
        target = self.root / "story.txt"
        target.write_text("one two three\nfour five six\n", encoding="utf-8")

        exact = json.loads(
            _edit_workspace_file(
                self.settings,
                self.context,
                {
                    "path": "story.txt",
                    "mode": "replace_text",
                    "old_text": "two",
                    "new_text": "TWO",
                },
            )
        )
        self.assertEqual(exact["replacements"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "one TWO three\nfour five six\n")

        ranged = json.loads(
            _edit_workspace_file(
                self.settings,
                self.context,
                {
                    "path": "story.txt",
                    "mode": "replace_range",
                    "start_line": 2,
                    "start_column": 6,
                    "end_line": 2,
                    "end_column": 10,
                    "new_text": "FIVE",
                },
            )
        )
        self.assertEqual(ranged["replacements"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "one TWO three\nfour FIVE six\n")

    def test_edit_workspace_file_rejects_ambiguous_exact_replacement(self) -> None:
        (self.root / "dupes.txt").write_text("same same\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "exactly once"):
            _edit_workspace_file(
                self.settings,
                self.context,
                {
                    "path": "dupes.txt",
                    "mode": "replace_text",
                    "old_text": "same",
                    "new_text": "changed",
                },
            )

    def test_copy_workspace_entry_copies_files_and_directories(self) -> None:
        (self.root / "source.txt").write_text("copy me", encoding="utf-8")
        copied_file = json.loads(
            _copy_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "source.txt", "destination_path": "nested/copied.txt"},
            )
        )
        self.assertTrue(copied_file["copied"])
        self.assertEqual(copied_file["type"], "file")
        self.assertEqual((self.root / "nested" / "copied.txt").read_text(encoding="utf-8"), "copy me")
        self.assertEqual((self.root / "source.txt").read_text(encoding="utf-8"), "copy me")

        (self.root / "dir").mkdir()
        (self.root / "dir" / "a.txt").write_text("A", encoding="utf-8")
        copied_dir = json.loads(
            _copy_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "dir", "destination_path": "dir-copy"},
            )
        )
        self.assertTrue(copied_dir["copied"])
        self.assertEqual(copied_dir["type"], "directory")
        self.assertEqual((self.root / "dir-copy" / "a.txt").read_text(encoding="utf-8"), "A")

    def test_move_workspace_entry_moves_files_and_rejects_self_nesting(self) -> None:
        (self.root / "old.txt").write_text("move me", encoding="utf-8")
        moved_file = json.loads(
            _move_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "old.txt", "destination_path": "renamed/new.txt"},
            )
        )
        self.assertTrue(moved_file["moved"])
        self.assertEqual(moved_file["type"], "file")
        self.assertFalse((self.root / "old.txt").exists())
        self.assertEqual((self.root / "renamed" / "new.txt").read_text(encoding="utf-8"), "move me")

        (self.root / "parent").mkdir()
        with self.assertRaisesRegex(ValueError, "directory into itself"):
            _move_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "parent", "destination_path": "parent/child"},
            )

    def test_copy_and_move_reject_existing_destination_without_overwrite(self) -> None:
        (self.root / "source.txt").write_text("source", encoding="utf-8")
        (self.root / "destination.txt").write_text("destination", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "already exists"):
            _copy_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "source.txt", "destination_path": "destination.txt"},
            )

        moved = json.loads(
            _move_workspace_entry(
                self.settings,
                self.context,
                {"source_path": "source.txt", "destination_path": "destination.txt", "overwrite": True},
            )
        )
        self.assertTrue(moved["moved"])
        self.assertEqual((self.root / "destination.txt").read_text(encoding="utf-8"), "source")
        self.assertFalse((self.root / "source.txt").exists())

    def test_zip_and_unzip_workspace_entries(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("A", encoding="utf-8")
        (self.root / "src" / "b.txt").write_text("B", encoding="utf-8")

        zipped = json.loads(
            _zip_workspace_entries(
                self.settings,
                self.context,
                {"paths": ["src"], "output_path": "bundle.zip"},
            )
        )
        self.assertEqual(zipped["entry_count"], 2)
        self.assertTrue((self.root / "bundle.zip").is_file())

        unzipped = json.loads(
            _unzip_workspace_archive(
                self.settings,
                self.context,
                {"archive_path": "bundle.zip", "output_dir": "out"},
            )
        )
        self.assertEqual(unzipped["entry_count"], 2)
        self.assertEqual((self.root / "out" / "src" / "a.txt").read_text(encoding="utf-8"), "A")
        self.assertEqual((self.root / "out" / "src" / "b.txt").read_text(encoding="utf-8"), "B")

    def test_unzip_workspace_archive_rejects_zip_slip_entries(self) -> None:
        archive_path = self.root / "bad.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.txt", "bad")

        with self.assertRaisesRegex(ValueError, "Unsafe zip entry path"):
            _unzip_workspace_archive(
                self.settings,
                self.context,
                {"archive_path": "bad.zip", "output_dir": "out"},
            )
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_publish_downloadable_file_uses_file_path_and_description(self) -> None:
        session_context = SimpleNamespace(session_id="sess-1")
        session_root = self.root / "sess-1"
        session_root.mkdir()
        (session_root / "story.html").write_text("<h1>Story</h1>", encoding="utf-8")

        result = json.loads(
            _publish_downloadable_file(
                self.settings,
                session_context,
                "/api/backend/downloads",
                {
                    "file_path": "story.html",
                    "description": "Story HTML",
                    "download_name": "story.html",
                },
            )
        )

        self.assertEqual(result["name"], "story.html")
        self.assertEqual(result["summary"], "Story HTML")
        self.assertEqual(result["download_url"], "/api/backend/downloads/sess-1/story.html")
        self.assertTrue((self.root / ".agent_framework" / "downloads" / "sess-1" / "story.html").is_file())

    def test_publish_downloadable_file_rejects_old_path_parameter(self) -> None:
        session_context = SimpleNamespace(session_id="sess-1")
        (self.root / "sess-1").mkdir()

        with self.assertRaisesRegex(ValueError, "file_path"):
            _publish_downloadable_file(
                self.settings,
                session_context,
                "/api/backend/downloads",
                {"path": "story.html"},
            )


if __name__ == "__main__":
    unittest.main()
