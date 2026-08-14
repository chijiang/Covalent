from __future__ import annotations

import unittest

from covalent.model.base import ModelProviderError
from covalent.model.openai_compatible import OpenAICompatibleProvider


class OpenAICompatibleProviderParseArgumentsTests(unittest.TestCase):
    def test_invalid_tool_arguments_raise_model_provider_error(self) -> None:
        with self.assertRaises(ModelProviderError) as context:
            OpenAICompatibleProvider._parse_arguments(
                '{"query":"abc""limit":5}',
                provider="openai_compatible",
                tool_name="search_docs",
            )

        error = context.exception
        self.assertEqual(error.status_code, 502)
        self.assertIn("tool 'search_docs' arguments", error.detail)
        self.assertIn("Expecting ',' delimiter", error.detail)
        self.assertIn("Around char", error.detail)

    def test_non_object_tool_arguments_raise_model_provider_error(self) -> None:
        with self.assertRaises(ModelProviderError) as context:
            OpenAICompatibleProvider._parse_arguments(
                '["a", "b"]',
                provider="openai_compatible",
                tool_name="search_docs",
            )

        error = context.exception
        self.assertEqual(error.status_code, 502)
        self.assertIn("Expected a JSON object", error.detail)


class OpenAICompatibleProviderContentNormalizationTests(unittest.TestCase):
    def test_adds_missing_type_to_provider_text_part(self) -> None:
        content = [{"text": "Important conversation result"}]

        normalized = OpenAICompatibleProvider._normalize_content_parts(content)

        self.assertEqual(normalized, [{"type": "text", "text": "Important conversation result"}])
        self.assertEqual(OpenAICompatibleProvider._extract_text(normalized), "Important conversation result")

    def test_preserves_valid_multimodal_content(self) -> None:
        content = [
            {"type": "text", "text": "Inspect this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]

        normalized = OpenAICompatibleProvider._normalize_content_parts(content)

        self.assertEqual(normalized, content)


if __name__ == "__main__":
    unittest.main()
