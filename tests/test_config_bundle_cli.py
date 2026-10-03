"""CLI surface tests for the typer app (no database needed)."""

from __future__ import annotations

import unittest

from click.utils import strip_ansi
from typer.testing import CliRunner

from covalent_enterprise.cli.app import app

runner = CliRunner()


class RootAppTests(unittest.TestCase):
    def test_root_help_lists_core_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        for command in ("serve", "migrate", "config", "users", "providers"):
            self.assertIn(command, result.output)

    def test_serve_help_keeps_argparse_compatible_options(self) -> None:
        result = runner.invoke(app, ["serve", "--help"])
        self.assertEqual(result.exit_code, 0)
        help_text = strip_ansi(result.output)
        self.assertIn("--host", help_text)
        self.assertIn("--port", help_text)
        self.assertIn("0.0.0.0", help_text)
        self.assertIn("5170", help_text)
        self.assertIn("AGENT_FRAMEWORK_BACKEND_PORT", help_text)

    def test_migrate_help_mentions_migrations(self) -> None:
        result = runner.invoke(app, ["migrate", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("migrations", result.output.lower())


class ConfigCommandTests(unittest.TestCase):
    def test_export_help_warns_about_plaintext_keys(self) -> None:
        result = runner.invoke(app, ["config", "export", "--help"])
        self.assertEqual(result.exit_code, 0)
        help_text = strip_ansi(result.output)
        self.assertIn("--output", help_text)
        self.assertIn("-o", help_text)
        self.assertIn("--no-skills", help_text)
        self.assertIn("plaintext", help_text.lower())

    def test_import_help_lists_options(self) -> None:
        result = runner.invoke(app, ["config", "import", "--help"])
        self.assertEqual(result.exit_code, 0)
        help_text = strip_ansi(result.output)
        for option in ("--on-conflict", "--dry-run", "--strict", "--no-skills"):
            self.assertIn(option, help_text)

    def test_import_rejects_missing_bundle_file(self) -> None:
        result = runner.invoke(app, ["config", "import", "/nonexistent/bundle.zip"])
        self.assertNotEqual(result.exit_code, 0)

    def test_import_rejects_invalid_on_conflict(self) -> None:
        result = runner.invoke(
            app, ["config", "import", "any.zip", "--on-conflict", "merge"]
        )
        self.assertNotEqual(result.exit_code, 0)


class UsersProvidersHelpTests(unittest.TestCase):
    def test_users_help_lists_commands(self) -> None:
        result = runner.invoke(app, ["users", "--help"])
        self.assertEqual(result.exit_code, 0)
        for command in ("list", "create", "set-role", "reset-password"):
            self.assertIn(command, result.output)

    def test_providers_help_lists_commands(self) -> None:
        result = runner.invoke(app, ["providers", "--help"])
        self.assertEqual(result.exit_code, 0)
        for command in ("list", "set-key"):
            self.assertIn(command, result.output)

    def test_providers_set_key_offers_stdin(self) -> None:
        result = runner.invoke(app, ["providers", "set-key", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--stdin", strip_ansi(result.output))


if __name__ == "__main__":
    unittest.main()
