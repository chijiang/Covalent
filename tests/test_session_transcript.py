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
    _request_display_input,
)
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


if __name__ == "__main__":
    unittest.main()
