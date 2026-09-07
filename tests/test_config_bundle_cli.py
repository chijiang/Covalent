"""CLI surface tests for the typer app (no database needed)."""

from __future__ import annotations

import unittest

from typer.testing import CliRunner

from covalent.cli.app import app

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
        self.assertIn("--host", result.output)
        self.assertIn("--port", result.output)
        self.assertIn("0.0.0.0", result.output)
        self.assertIn("5170", result.output)
        self.assertIn("AGENT_FRAMEWORK_BACKEND_PORT", result.output)

    def test_migrate_help_mentions_migrations(self) -> None:
        result = runner.invoke(app, ["migrate", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("migrations", result.output.lower())


class ConfigCommandTests(unittest.TestCase):
    def test_export_help_warns_about_plaintext_keys(self) -> None:
        result = runner.invoke(app, ["config", "export", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("--output", result.output)
        self.assertIn("-o", result.output)
        self.assertIn("--no-skills", result.output)
        self.assertIn("plaintext", result.output.lower())

    def test_import_help_lists_options(self) -> None:
        result = runner.invoke(app, ["config", "import", "--help"])
        self.assertEqual(result.exit_code, 0)
        for option in ("--on-conflict", "--dry-run", "--strict", "--no-skills"):
            self.assertIn(option, result.output)

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
        self.assertIn("--stdin", result.output)


if __name__ == "__main__":
    unittest.main()
