"""Enterprise-side anthropic_compatible provider handling.

- ``PersistedProviderConfig`` coerces ``api_style`` to ``"messages"`` for
  anthropic providers (imports and form payloads may omit or mis-set it) and
  rejects the ``messages`` style on non-Anthropic provider types.
- ``_resolve_default_provider`` passes the anthropic provider type and style
  through to the runtime ``ProviderConfig`` untouched.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from covalent_enterprise.application.services.management_service import _resolve_default_provider
from covalent_enterprise.infra.config_store import PersistedProviderConfig
from covalent_enterprise.infra.settings import AppSettings


def _settings() -> AppSettings:
    return AppSettings(console_auth_mode="dev", workspace_root_dir="/tmp")


class PersistedProviderStyleTests(unittest.TestCase):
    def test_anthropic_coerces_style_from_none(self) -> None:
        config = PersistedProviderConfig(
            name="claude",
            provider_type="anthropic_compatible",
            base_url="https://api.anthropic.com",
        )
        self.assertEqual(config.api_style, "messages")

    def test_anthropic_coerces_wrong_style(self) -> None:
        config = PersistedProviderConfig(
            name="claude",
            provider_type="anthropic_compatible",
            base_url="https://api.anthropic.com",
            api_style="responses",
        )
        self.assertEqual(config.api_style, "messages")

    def test_openai_rejects_messages_style(self) -> None:
        with self.assertRaises(ValidationError):
            PersistedProviderConfig(
                name="openai",
                provider_type="openai_compatible",
                base_url="https://api.openai.com/v1",
                api_style="messages",
            )

    def test_openai_keeps_existing_styles(self) -> None:
        for style in ("chat_completions", "responses", None):
            with self.subTest(style=style):
                config = PersistedProviderConfig(
                    name="openai",
                    provider_type="openai_compatible",
                    base_url="https://api.openai.com/v1",
                    api_style=style,
                )
                self.assertEqual(config.api_style, style)


class ResolveDefaultProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_anthropic_default_provider_passes_through(self) -> None:
        payload = [{
            "name": "claude",
            "provider_type": "anthropic_compatible",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-test",
            "default_model": "claude-test",
            "is_default": False,
            "position": 0,
        }]
        provider = await _resolve_default_provider(_settings(), None, payload)
        self.assertEqual(provider.provider, "anthropic_compatible")
        self.assertEqual(provider.api_style, "messages")
        self.assertEqual(provider.model, "claude-test")
        self.assertEqual(provider.base_url, "https://api.anthropic.com")
        self.assertEqual(provider.api_key, "sk-ant-test")


if __name__ == "__main__":
    unittest.main()
