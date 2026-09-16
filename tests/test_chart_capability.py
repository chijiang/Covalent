"""Chart output capability: prompt injection and config round-trip.

The ``chart`` capability is off by default — a general agent's system prompt
must not mention echarts. When enabled, the runtime injects the echarts block
policy so the model knows how to emit chart specs, and the capability
persists through PersistedAgentConfig like any other capability.
"""

from __future__ import annotations

import unittest

from covalent.core.types import Capability
from covalent.infra.config_store import PersistedAgentConfig
from covalent.runtime.react import CHART_CAPABILITY_POLICY, ReactAgentRuntime

from tests.helpers import make_test_agent, make_test_registry, make_test_runtime


def _system_prompt(capabilities: set[Capability]) -> str:
    agent = make_test_agent(capabilities=capabilities)
    runtime: ReactAgentRuntime = make_test_runtime(make_test_registry(agent))
    return runtime._build_system_prompt(agent)


class ChartPromptInjectionTests(unittest.TestCase):
    def test_chart_capability_injects_echarts_policy(self) -> None:
        capabilities = {Capability.CHAT, Capability.REACT, Capability.CHART}
        prompt = _system_prompt(capabilities)
        self.assertIn(CHART_CAPABILITY_POLICY, prompt)
        self.assertIn("echarts", prompt)

    def test_default_agent_prompt_has_no_echarts_policy(self) -> None:
        prompt = _system_prompt({Capability.CHAT, Capability.REACT})
        self.assertNotIn(CHART_CAPABILITY_POLICY, prompt)
        self.assertNotIn("echarts", prompt)


class ChartCapabilityRoundTripTests(unittest.TestCase):
    def test_persisted_config_accepts_chart_capability(self) -> None:
        config = PersistedAgentConfig.model_validate({
            "name": "chart-agent",
            "description": "Chart agent",
            "system_prompt": "You draw charts.",
            "provider": {"provider": "test", "model": "m"},
            "capabilities": ["chat", "react", "chart"],
        })
        self.assertIn(Capability.CHART, config.capabilities)
        dumped = config.model_dump(mode="json")
        self.assertIn("chart", dumped["capabilities"])

    def test_default_capabilities_exclude_chart(self) -> None:
        config = PersistedAgentConfig.model_validate({
            "name": "plain-agent",
            "description": "General agent",
            "system_prompt": "You help.",
            "provider": {"provider": "test", "model": "m"},
        })
        self.assertNotIn(Capability.CHART, config.capabilities)


if __name__ == "__main__":
    unittest.main()
