"""Unit tests for the SSE download-artifact extraction in ReactAgentRuntime.

`_extract_download_artifacts` pulls saved-file JSON text parts out of
multimodal tool results so the chat pipeline can turn them into attachments
without the 400-char SSE summary truncation cutting a download_url.
"""

from __future__ import annotations

import json
import unittest

from covalent.runtime.react import ReactAgentRuntime


def _screenshot_content() -> list[dict]:
    metadata = {
        "id": "download-s1-shot.png",
        "name": "shot.png",
        "size": 4096,
        "content_type": "image/png",
        "download_url": "/api/backend/downloads/s1/shot.png",
        "download_markdown": "[Download shot.png](/api/backend/downloads/s1/shot.png)",
        "summary": "Screenshot of https://example.com/ (viewport)",
    }
    return [
        {"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


class ExtractDownloadArtifactsTests(unittest.TestCase):
    def test_artifact_pulled_out_and_leading_line_added(self) -> None:
        artifacts, remaining = ReactAgentRuntime._extract_download_artifacts(_screenshot_content())
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["name"], "shot.png")
        self.assertEqual(artifacts[0]["download_url"], "/api/backend/downloads/s1/shot.png")
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0], {"type": "text", "text": "Saved file: /api/backend/downloads/s1/shot.png"})
        self.assertEqual(remaining[1]["type"], "image_url")

    def test_summary_shows_saved_line_and_image_omitted(self) -> None:
        _, remaining = ReactAgentRuntime._extract_download_artifacts(_screenshot_content())
        summary = ReactAgentRuntime._event_tool_content_summary(remaining)
        self.assertIn("Saved file: /api/backend/downloads/s1/shot.png", summary)
        self.assertIn("[1 image omitted]", summary)
        self.assertNotIn("download_markdown", summary)

    def test_no_artifacts_content_unchanged(self) -> None:
        content = [
            {"type": "text", "text": "plain result"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        artifacts, remaining = ReactAgentRuntime._extract_download_artifacts(content)
        self.assertEqual(artifacts, [])
        self.assertEqual(remaining, content)

    def test_json_without_download_url_stays_in_content(self) -> None:
        content = [{"type": "text", "text": json.dumps({"ok": True, "rows": 3})}]
        artifacts, remaining = ReactAgentRuntime._extract_download_artifacts(content)
        self.assertEqual(artifacts, [])
        self.assertEqual(remaining, content)

    def test_multiple_artifacts_each_get_a_line(self) -> None:
        content = _screenshot_content()
        content.append(dict(content[0], text=json.dumps({**json.loads(content[0]["text"]), "name": "shot-2.png", "download_url": "/api/backend/downloads/s1/shot-2.png"})))
        artifacts, remaining = ReactAgentRuntime._extract_download_artifacts(content)
        self.assertEqual(len(artifacts), 2)
        summary = ReactAgentRuntime._event_tool_content_summary(remaining)
        self.assertIn("shot.png", summary)
        self.assertIn("shot-2.png", summary)


if __name__ == "__main__":
    unittest.main()
