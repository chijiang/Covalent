"""Unit tests for the one-time agent-name migration's pure logic."""

from __future__ import annotations

import unittest

from scripts.migrate_agent_names import sanitize_agent_name, unique_sanitized


class SanitizeAgentNameTests(unittest.TestCase):
    def test_spaced_name_collapses_to_dashes(self) -> None:
        self.assertEqual(sanitize_agent_name("Random Speech Maker"), "Random-Speech-Maker")

    def test_dotted_name_folds(self) -> None:
        self.assertEqual(sanitize_agent_name("my.agent"), "my-agent")

    def test_non_ascii_name_falls_back(self) -> None:
        self.assertEqual(sanitize_agent_name("研究助手"), "agent")

    def test_valid_name_unchanged(self) -> None:
        self.assertEqual(sanitize_agent_name("research-router_2"), "research-router_2")

    def test_scoped_suffix_preserved(self) -> None:
        self.assertEqual(
            sanitize_agent_name("Random Speech Maker__user_u1"),
            "Random-Speech-Maker__user_u1",
        )

    def test_scoped_suffix_dots_folded(self) -> None:
        self.assertEqual(
            sanitize_agent_name("My Agent__user_a.b"),
            "My-Agent__user_a-b",
        )


class UniqueSanitizedNameTests(unittest.TestCase):
    def test_no_collision_returns_sanitized(self) -> None:
        self.assertEqual(unique_sanitized("Random Speech Maker", {"other"}), "Random-Speech-Maker")

    def test_collision_appends_counter(self) -> None:
        name = unique_sanitized("Random Speech Maker", {"Random-Speech-Maker"})
        self.assertEqual(name, "Random-Speech-Maker-2")

    def test_collision_counter_gaps(self) -> None:
        name = unique_sanitized("a b", {"a-b", "a-b-2"})
        self.assertEqual(name, "a-b-3")

    def test_collision_keeps_scoped_suffix_last(self) -> None:
        name = unique_sanitized("a b__user_u1", {"a-b__user_u1"})
        self.assertEqual(name, "a-b-2__user_u1")


if __name__ == "__main__":
    unittest.main()
