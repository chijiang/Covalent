from __future__ import annotations

import unittest

from agent_framework.model.base import ModelProviderError
from agent_framework.model.openai_compatible import OpenAICompatibleProvider


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


if __name__ == "__main__":
    unittest.main()
