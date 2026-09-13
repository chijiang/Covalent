"""Unit tests for transcript / resume-input use cases in session_service.

Regression guard: ``_build_user_transcript_message`` / ``_build_resume_tool_result``
shipped referencing an undefined ``request`` name after the ``_session_helpers``
extraction — the console streaming route crashed with ``NameError`` at runtime
while the fake-config API tests never exercised these helpers.
"""

from __future__ import annotations

import unittest

from covalent.application.errors import ConflictError
from covalent.application.services.session_service import (
    AgentRunInput,
    _build_resume_tool_result,
    _build_user_transcript_message,
    _reasoning_active_source,
    _reasoning_source_marker,
    _request_display_input,
    _upsert_assistant_reasoning,
)
from covalent.application.services.session_service import ChatTranscriptMessage
from covalent.core.types import UserInputRequest


class SessionTranscriptTestCase(unittest.TestCase):
    def test_request_display_input_plain_text(self) -> None:
        self.assertEqual(_request_display_input(AgentRunInput(input="hello")), "hello")

    def test_request_display_input_metadata_override(self) -> None:
        run = AgentRunInput(input="", metadata={"display_input": "  shown  "})
        self.assertEqual(_request_display_input(run), "shown")

    def test_request_display_input_content_parts(self) -> None:
        run = AgentRunInput(
            input=[
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]
        )
        self.assertEqual(_request_display_input(run), "first\n\nsecond")

    def test_request_display_input_images(self) -> None:
        run = AgentRunInput(input=[{"type": "image_url", "url": "x"}])
        self.assertEqual(_request_display_input(run), "Shared 1 image attachment.")

    def test_request_display_input_empty_fallback(self) -> None:
        run = AgentRunInput(input=[])
        self.assertEqual(_request_display_input(run), "Message sent.")

    def test_build_user_transcript_message(self) -> None:
        message = _build_user_transcript_message(
            AgentRunInput(input="hello", metadata={"user_message_id": "um-1"})
        )
        self.assertEqual(message.role, "user")
        self.assertEqual(message.content, "hello")
        self.assertEqual(message.id, "um-1")

    def test_build_user_transcript_message_empty_fallback(self) -> None:
        message = _build_user_transcript_message(AgentRunInput(input=[]))
        self.assertEqual(message.content, "Message sent.")

    def test_build_resume_tool_result(self) -> None:
        pending = UserInputRequest(id="q1", tool_call_id="tc-1", tool_name="ask_question")
        result = _build_resume_tool_result(
            AgentRunInput(
                input="my answer",
                metadata={"resume_question_id": "q1", "question_response": {"x": 1}},
            ),
            pending_input=pending,
        )
        self.assertEqual(result.tool_call_id, "tc-1")
        self.assertEqual(result.request_id, "q1")
        self.assertEqual(result.answers, {"x": 1})
        self.assertEqual(result.summary, "my answer")

    def test_build_resume_tool_result_without_pending_input(self) -> None:
        run = AgentRunInput(input="answer", metadata={"resume_question_id": "q1"})
        with self.assertRaises(ConflictError):
            _build_resume_tool_result(run, pending_input=None)

    def test_build_resume_tool_result_no_resume_question(self) -> None:
        run = AgentRunInput(input="plain message")
        self.assertIsNone(_build_resume_tool_result(run, pending_input=None))


class AssistantReasoningAttributionTestCase(unittest.TestCase):
    def _message(self) -> ChatTranscriptMessage:
        return ChatTranscriptMessage(id="am-1", role="assistant", content="")

    def test_main_then_delegate_then_main_writes_switch_markers(self) -> None:
        messages = [self._message()]
        _upsert_assistant_reasoning(messages, "am-1", "thinking ")
        _upsert_assistant_reasoning(messages, "am-1", "sub thinking", source="voc-agent")
        _upsert_assistant_reasoning(messages, "am-1", "more main")
        expected = (
            "thinking "
            + _reasoning_source_marker("voc-agent")
            + "sub thinking"
            + _reasoning_source_marker(None)
            + "more main"
        )
        self.assertEqual(messages[0].reasoning_content, expected)

    def test_same_source_consecutive_deltas_write_no_marker(self) -> None:
        messages = [self._message()]
        _upsert_assistant_reasoning(messages, "am-1", "a", source="voc-agent")
        _upsert_assistant_reasoning(messages, "am-1", "b", source="voc-agent")
        self.assertEqual(messages[0].reasoning_content, _reasoning_source_marker("voc-agent") + "ab")

    def test_lazy_creates_message_with_leading_marker(self) -> None:
        messages: list[ChatTranscriptMessage] = []
        _upsert_assistant_reasoning(messages, "am-1", "sub", source="voc-agent")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "")
        self.assertEqual(messages[0].reasoning_content, _reasoning_source_marker("voc-agent") + "sub")

    def test_active_source_none_for_legacy_content(self) -> None:
        self.assertIsNone(_reasoning_active_source("plain reasoning from an old session"))

    def test_active_source_round_trip(self) -> None:
        self.assertIsNone(_reasoning_active_source("plain legacy"))
        self.assertEqual(_reasoning_active_source("main" + _reasoning_source_marker("voc") + "sub"), "voc")
        self.assertIsNone(
            _reasoning_active_source("main" + _reasoning_source_marker("voc") + "sub" + _reasoning_source_marker(None))
        )

    def test_marker_is_control_char_wrapped(self) -> None:
        marker = _reasoning_source_marker("voc")
        self.assertTrue(marker.startswith("\x1e"))
        self.assertTrue(marker.endswith("\x1e"))
        self.assertIn("#agent:voc", marker)


if __name__ == "__main__":
    unittest.main()
