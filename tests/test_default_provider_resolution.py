"""Console-first provider resolution and first-boot seeding semantics.

Providers, agents, MCP servers, and skill sources are configured in the
Service Console (persisted in PostgreSQL); there are no env-var seeds or
env-var provider fallbacks. These tests pin that behavior:

- ``_resolve_default_provider`` reads only the providers document. With no
  providers registered it returns an empty-shell config (model/key/base_url
  unset) so a freshly seeded default agent backfills from whatever provider
  is registered in the console later.
- The first-boot default agent is built from hardcoded module constants.
- Config-document examples shown in the console are static.
"""

from __future__ import annotations

import json
import unittest

from covalent.application.services.management_service import (
    DEFAULT_AGENT_DESCRIPTION,
    DEFAULT_AGENT_MAX_ITERATIONS,
    DEFAULT_AGENT_SYSTEM_PROMPT,
    _example_config_raw,
    _resolve_default_provider,
    _seed_agent_payload,
)
from covalent.infra.settings import AppSettings
from covalent.model.base import ProviderConfig


def _settings() -> AppSettings:
    return AppSettings(console_auth_mode="dev", workspace_root_dir="/tmp")


class ResolveDefaultProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_providers_yields_empty_shell(self) -> None:
        provider = await _resolve_default_provider(_settings(), None, [])
        self.assertEqual(provider.provider, "openai_compatible")
        self.assertEqual(provider.model, "")
        self.assertIsNone(provider.api_key)
        self.assertIsNone(provider.base_url)

    async def test_provider_with_default_model_wins(self) -> None:
        payload = [{
            "name": "deepseek",
            "provider_type": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
            "default_model": "deepseek-v4-flash",
            "is_default": False,
            "position": 0,
        }]
        provider = await _resolve_default_provider(_settings(), None, payload)
        self.assertEqual(provider.model, "deepseek-v4-flash")
        self.assertEqual(provider.api_key, "sk-test")
        self.assertEqual(provider.base_url, "https://api.deepseek.com")

    async def test_is_default_without_default_model_resolves_empty_model(self) -> None:
        payload = [{
            "name": "qwen",
            "provider_type": "openai_compatible",
            "base_url": "https://dashscope.example/compatible-mode/v1",
            "api_key": "sk-test",
            "default_model": "",
            "is_default": True,
            "position": 0,
        }]
        provider = await _resolve_default_provider(_settings(), None, payload)
        self.assertEqual(provider.model, "")
        self.assertEqual(provider.base_url, "https://dashscope.example/compatible-mode/v1")


class SeedAgentPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_agent_uses_constants_and_resolved_provider(self) -> None:
        provider = ProviderConfig(provider="openai_compatible", model="", api_key=None, base_url=None)
        payload = _seed_agent_payload(_settings(), provider, [])
        self.assertEqual(len(payload), 1)
        agent = payload[0]
        self.assertEqual(agent["name"], "default")
        self.assertEqual(agent["description"], DEFAULT_AGENT_DESCRIPTION)
        self.assertEqual(agent["system_prompt"], DEFAULT_AGENT_SYSTEM_PROMPT)
        self.assertEqual(agent["max_iterations"], DEFAULT_AGENT_MAX_ITERATIONS)
        # Empty-shell provider passes through so the agent backfills from the
        # console-registered default provider at runtime.
        self.assertEqual(agent["provider"]["model"], "")
        self.assertIsNone(agent["provider"]["base_url"])
        self.assertIn("get_current_time", agent["local_tools"])


class ExampleConfigTests(unittest.TestCase):
    def test_examples_are_static_valid_json(self) -> None:
        for kind in ("agents", "mcp", "skill_sources"):
            with self.subTest(kind=kind):
                payload = json.loads(_example_config_raw(kind))
                self.assertIsInstance(payload, list)
                self.assertTrue(payload, f"{kind} example should not be empty")


if __name__ == "__main__":
    unittest.main()
