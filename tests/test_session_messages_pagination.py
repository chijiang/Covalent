"""Tests for GET /sessions/{id} message pagination
(messages_limit / messages_before query params)."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, UTC

from tests.test_session_activity_api import _admin_cookie, _build_app_with_store
from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage, InMemorySessionStore


def _seed_session(store: InMemorySessionStore, session_id: str) -> None:
    """Seed [um-1 user, am-1 assistant, um-2 user, am-2 assistant]."""
    asyncio.run(store.save_session(
        ChatSessionRecord(
            id=session_id,
            title="t",
            title_source="auto",
            agent_name="default",
            owner_user_id=None,
            workspace_id=None,
            preview_text="",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            memory_messages=[],
            messages=[
                ChatTranscriptMessage(id="um-1", role="user", content="first"),
                ChatTranscriptMessage(id="am-1", role="assistant", content="reply 1"),
                ChatTranscriptMessage(id="um-2", role="user", content="second"),
                ChatTranscriptMessage(id="am-2", role="assistant", content="reply 2"),
            ],
            activity=[],
        )
    ))


def _get_session(client, headers, session_id="sess-1", **params):
    return client.get(f"/sessions/{session_id}", params=params or None, headers=headers)


class SessionMessagesPaginationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from covalent.infra.memory import InMemorySessionStore

        self.store = InMemorySessionStore()
        _seed_session(self.store, "sess-1")
        self.client = _build_app_with_store(self.store)
        self.headers = {"Cookie": _admin_cookie(self.client.app)}

    def test_no_params_returns_full_transcript(self) -> None:
        resp = _get_session(self.client, self.headers)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual([m["id"] for m in body["messages"]], ["um-1", "am-1", "um-2", "am-2"])
        self.assertEqual(body["messages_total"], 4)
        self.assertFalse(body["messages_has_more"])
        self.assertEqual([m["position"] for m in body["messages"]], [0, 1, 2, 3])

    def test_limit_returns_newest_page(self) -> None:
        resp = _get_session(self.client, self.headers, messages_limit=2)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual([m["id"] for m in body["messages"]], ["um-2", "am-2"])
        self.assertEqual([m["position"] for m in body["messages"]], [2, 3])
        self.assertEqual(body["messages_total"], 4)
        self.assertTrue(body["messages_has_more"])

    def test_before_pages_older_messages(self) -> None:
        resp = _get_session(self.client, self.headers, messages_limit=2, messages_before=2)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual([m["id"] for m in body["messages"]], ["um-1", "am-1"])
        self.assertEqual([m["position"] for m in body["messages"]], [0, 1])
        self.assertEqual(body["messages_total"], 4)
        self.assertFalse(body["messages_has_more"])

    def test_limit_larger_than_total_returns_everything(self) -> None:
        resp = _get_session(self.client, self.headers, messages_limit=100)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["messages"]), 4)
        self.assertFalse(body["messages_has_more"])

    def test_limit_below_one_rejected(self) -> None:
        resp = _get_session(self.client, self.headers, messages_limit=0)

        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
