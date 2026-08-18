"""Unit tests for the delegate lifecycle SSE event constants.

The constants are the API layer's contract with stream consumers (the route
persists any event whose name is in TRACE_ACTIVITY_EVENTS), so every lifecycle
name must be prefix-consistent, short enough for the activity title column
(≤64 chars), and registered for persistence.
"""

import unittest

from covalent.api.sse_events import (
    SSE_EVENT_DELEGATE_CANCELLED,
    SSE_EVENT_DELEGATE_CREATED,
    SSE_EVENT_DELEGATE_EXPIRED,
    SSE_EVENT_DELEGATE_FAILED,
    SSE_EVENT_DELEGATE_IDLE,
    SSE_EVENT_DELEGATE_RELEASED,
    SSE_EVENT_DELEGATE_RESUMED,
    SSE_EVENT_DELEGATE_RUNNING,
    SSE_EVENT_DELEGATE_WAITING_PARENT,
    TRACE_ACTIVITY_EVENTS,
)

DELEGATE_LIFECYCLE_SSE_EVENTS = (
    SSE_EVENT_DELEGATE_CREATED,
    SSE_EVENT_DELEGATE_RUNNING,
    SSE_EVENT_DELEGATE_WAITING_PARENT,
    SSE_EVENT_DELEGATE_RESUMED,
    SSE_EVENT_DELEGATE_IDLE,
    SSE_EVENT_DELEGATE_RELEASED,
    SSE_EVENT_DELEGATE_CANCELLED,
    SSE_EVENT_DELEGATE_EXPIRED,
    SSE_EVENT_DELEGATE_FAILED,
)


class DelegateLifecycleSSEEventTests(unittest.TestCase):
    def test_lifecycle_events_are_delegate_prefixed_and_short(self) -> None:
        for name in DELEGATE_LIFECYCLE_SSE_EVENTS:
            self.assertTrue(
                name.startswith("delegate_"),
                f"{name!r} must start with the delegate_ prefix",
            )
            self.assertLessEqual(
                len(name), 64, f"{name!r} exceeds the 64-char activity title limit"
            )

    def test_lifecycle_events_are_persisted_as_activity(self) -> None:
        for name in DELEGATE_LIFECYCLE_SSE_EVENTS:
            self.assertIn(
                name, TRACE_ACTIVITY_EVENTS, f"{name!r} must be in TRACE_ACTIVITY_EVENTS"
            )

    def test_nine_distinct_lifecycle_events(self) -> None:
        self.assertEqual(len(DELEGATE_LIFECYCLE_SSE_EVENTS), 9)
        self.assertEqual(len(set(DELEGATE_LIFECYCLE_SSE_EVENTS)), 9)


if __name__ == "__main__":
    unittest.main()
