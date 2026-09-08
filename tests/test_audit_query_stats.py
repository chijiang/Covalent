"""Unit tests for audit-log user query stats (build_query_stats + endpoint auth)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from covalent.application.errors import ForbiddenError
from covalent.application.services.audit_service import (
    _stat_type,
    build_query_stats,
    get_user_query_stats,
)
from covalent.infra.db import UserRow

_NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _row(day: str, user_id: str, action: str, outcome: str, count: int = 1, last_at=None):
    return (day, user_id, action, outcome, count, last_at or datetime(2026, 9, int(day[-2:]), 10, 0, tzinfo=UTC))


class StatTypeTests(unittest.TestCase):
    def test_public_invoke_outcomes_map_to_correct_buckets(self) -> None:
        self.assertEqual(_stat_type("agent.invoke", "completed"), "query")
        self.assertEqual(_stat_type("agent.invoke", "failed"), "failed")
        self.assertEqual(_stat_type("agent.invoke", "aborted"), "failed")

    def test_console_invoke_action_uses_success_outcome(self) -> None:
        self.assertEqual(_stat_type("agent.invoke.completed", "success"), "query")
        self.assertEqual(_stat_type("agent.invoke.completed", "failed"), "failed")

    def test_denied_action_is_denied(self) -> None:
        self.assertEqual(_stat_type("agent.invoke.denied", "denied"), "denied")


class BuildQueryStatsTests(unittest.TestCase):
    def test_counts_per_day_and_user(self) -> None:
        rows = [
            _row("2026-09-07", "u1", "agent.invoke", "completed", 3),
            _row("2026-09-07", "u1", "agent.invoke", "failed", 1),
            _row("2026-09-07", "u1", "agent.invoke.denied", "denied", 2),
            _row("2026-09-06", "u2", "agent.invoke", "completed", 5),
        ]
        labels = {
            "u1": {"email": "u1@test", "display_name": "User One"},
            "u2": {"email": "u2@test", "display_name": "User Two"},
        }
        stats = build_query_stats(rows, labels, days=7, now=_NOW)

        self.assertEqual(stats.days, 7)
        self.assertEqual(len(stats.users), 2)
        top = stats.users[0]
        self.assertEqual(top.user_id, "u2")
        self.assertEqual(top.total_query_count, 5)
        second = stats.users[1]
        self.assertEqual(second.user_id, "u1")
        self.assertEqual(second.total_query_count, 3)
        self.assertEqual(second.total_failed_count, 1)
        self.assertEqual(second.total_denied_count, 2)
        day = next(entry for entry in second.daily if entry.date == "2026-09-07")
        self.assertEqual((day.query_count, day.denied_count, day.failed_count), (3, 2, 1))

    def test_daily_is_dense_with_zero_fill(self) -> None:
        rows = [_row("2026-09-08", "u1", "agent.invoke", "completed", 2)]
        stats = build_query_stats(rows, {}, days=7, now=_NOW)
        self.assertEqual(len(stats.users[0].daily), 7)
        quiet = [entry for entry in stats.users[0].daily if entry.query_count == 0]
        self.assertEqual(len(quiet), 6)

    def test_rows_outside_range_are_ignored(self) -> None:
        rows = [
            _row("2026-08-01", "u1", "agent.invoke", "completed", 9),
            _row("2026-09-08", "u1", "agent.invoke", "completed", 1),
        ]
        stats = build_query_stats(rows, {}, days=7, now=_NOW)
        self.assertEqual(stats.users[0].total_query_count, 1)

    def test_days_bounds_are_clamped(self) -> None:
        stats_low = build_query_stats([], {}, days=0, now=_NOW)
        stats_high = build_query_stats([], {}, days=999, now=_NOW)
        self.assertEqual(stats_low.days, 1)
        self.assertEqual(stats_high.days, 365)

    def test_user_label_missing_falls_back_to_none(self) -> None:
        rows = [_row("2026-09-08", "ghost", "agent.invoke", "completed")]
        stats = build_query_stats(rows, {}, days=1, now=_NOW)
        user = stats.users[0]
        self.assertIsNone(user.email)
        self.assertIsNone(user.display_name)
        self.assertEqual(user.last_query_at is not None, True)

    def test_sorted_by_query_count_desc(self) -> None:
        rows = [
            _row("2026-09-08", "a", "agent.invoke", "completed", 1),
            _row("2026-09-08", "b", "agent.invoke", "completed", 9),
        ]
        stats = build_query_stats(rows, {}, days=1, now=_NOW)
        self.assertEqual([user.user_id for user in stats.users], ["b", "a"])


class GetUserQueryStatsAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_principal_raises_forbidden(self) -> None:
        principal = SimpleNamespace(is_admin=False)
        db_manager = SimpleNamespace(session_factory=lambda: None)
        with self.assertRaises(ForbiddenError):
            await get_user_query_stats(db_manager, principal, days=30)

    async def test_admin_principal_queries_and_aggregates(self) -> None:
        rows = [
            ("2026-09-08", "u1", "agent.invoke", "completed", 4, datetime(2026, 9, 8, 9, 0, tzinfo=UTC)),
        ]

        class _Result:
            def __init__(self, payload):
                self._payload = payload

            def all(self):
                return self._payload

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, stmt):
                entities = {d.get("entity") for d in stmt.column_descriptions or []}
                if UserRow in entities:
                    return _Result([("u1", "u1@test", "User One")])
                return _Result(rows)

        principal = SimpleNamespace(is_admin=True, workspace_id="ws-1")
        db_manager = SimpleNamespace(session_factory=lambda: _Session())
        stats = await get_user_query_stats(db_manager, principal, days=7)
        self.assertEqual(len(stats.users), 1)
        self.assertEqual(stats.users[0].email, "u1@test")
        self.assertEqual(stats.users[0].total_query_count, 4)


if __name__ == "__main__":
    unittest.main()
