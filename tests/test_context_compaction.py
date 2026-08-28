"""Unit tests for ContextWindowManager compaction policy.

Regression guards for three review items:
- X5: the early-out must not trust a stale ``last_prompt_tokens`` so much that
  it skips compaction when the current conversation is actually over budget.
- M4: LLM summarization failures must be logged (not silently swallowed), and
  the expensive LLM summary must not be re-invoked on every iteration once a
  run has already produced a compaction summary.
"""

from __future__ import annotations

import unittest
from typing import Any

from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationResponse, Message
from covalent.model.base import ProviderConfig
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.context_window_manager import ContextWindowManager

from tests.helpers import ScriptedModelAdapter


def _manager_with_budget(
    *,
    token_budget: int,
    compact_threshold: float,
    recent_messages: int,
    enable_llm_summarization: bool = True,
) -> tuple[ContextWindowManager, ScriptedModelAdapter, AgentSpec]:
    class _RuntimeStub:
        def _serialize_content(self, content: Any) -> str:
            return content if isinstance(content, str) else str(content)

        def _normalize_summary_text(self, text: str) -> str:
            return " ".join(text.split())

        def _truncate_text(self, text: str, max_chars: int) -> str:
            return text if len(text) <= max_chars else text[:max_chars]

        def _safe_json_dumps(self, value: Any) -> str:
            import json
            try:
                return json.dumps(value, ensure_ascii=False)
            except TypeError:
                return str(value)

    adapter = ScriptedModelAdapter([GenerationResponse(model="test-model", output_text="compaction summary")])
    registry = FrameworkRegistry()
    agent = AgentSpec(
        name="test",
        description="d",
        system_prompt="s",
        provider=ProviderConfig(provider="test", model="test-model"),
        context_window=token_budget,
    )
    registry.register_agent(agent)
    registry.model_providers[agent.provider.cache_key()] = adapter

    stub = _RuntimeStub()
    stub.registry = registry  # type: ignore[attr-defined]
    mgr = ContextWindowManager(
        stub,  # type: ignore[arg-type]
        session_history_limit=1000,
        context_compact_threshold=compact_threshold,
        context_recent_messages=recent_messages,
        context_summary_char_budget=2000,
        context_message_char_limit=100_000,
        context_min_recent_messages=1,
        context_summary_model=None,
        enable_llm_summarization=enable_llm_summarization,
    )
    return mgr, adapter, agent


class CompactionPolicyTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_x5_stale_last_prompt_tokens_does_not_skip_compaction(self) -> None:
        """X5: a stale (small) last_prompt_tokens must not suppress compaction
        when the current message set is clearly over the char-based estimate.

        Token budget 1000, threshold 0.75 -> trigger at 750 tokens. Build a
        conversation whose char-based estimate is well above 750, but pass a
        stale last_prompt_tokens=10. The old code took last_prompt_tokens
        verbatim and early-returned "no compaction"; the fix takes the max.
        """
        mgr, _adapter, agent = _manager_with_budget(token_budget=1000, compact_threshold=0.75, recent_messages=2)
        # ~3.5 chars/token -> need > 750*3.5 ~= 2625 chars to exceed threshold.
        big = "x" * 4000
        messages = [Message(role="user", content=big), Message(role="assistant", content=big)]

        result, stats = await mgr._compact_generation_messages(messages, agent=agent, last_prompt_tokens=10)

        self.assertTrue(stats["compacted"], "compaction must fire despite stale small last_prompt_tokens")

    async def test_x5_under_budget_does_not_compact(self) -> None:
        """Sanity: a genuinely small conversation still skips compaction."""
        mgr, _adapter, agent = _manager_with_budget(token_budget=1000, compact_threshold=0.75, recent_messages=2)
        messages = [Message(role="user", content="hi")]
        result, stats = await mgr._compact_generation_messages(messages, agent=agent, last_prompt_tokens=None)
        self.assertFalse(stats["compacted"])

    async def test_m4_llm_summary_not_re_invoked_after_first_compaction(self) -> None:
        """M4 debounce: once a compaction summary is present in the older set,
        subsequent compactions in the same run must NOT call the LLM again.

        We craft older_messages to already contain a system summary (the marker
        the manager recognizes), plus enough volume to need compaction. The
        counting adapter must be called 0 times (falls back to local heuristic).
        """
        mgr, adapter, agent = _manager_with_budget(
            token_budget=200, compact_threshold=0.5, recent_messages=1, enable_llm_summarization=True,
        )
        big = "y" * 2000
        older = [
            Message(role="system", content="Earlier conversation context was compacted to stay within the model budget. notes."),
            Message(role="user", content=big),
            Message(role="assistant", content=big),
        ]
        result, stats = await mgr._compact_generation_messages(older, agent=agent, last_prompt_tokens=None)
        self.assertTrue(stats["compacted"])
        self.assertEqual(adapter.call_count, 0, "LLM summary must be debounced when a summary already exists")

    async def test_m4_llm_summary_invoked_on_first_crossing(self) -> None:
        """Complement to the debounce test: with no prior summary in the older
        set, ``_llm_summarize_messages`` does invoke the model adapter (proves
        the LLM path works and isn't accidentally dead)."""
        mgr, adapter, agent = _manager_with_budget(
            token_budget=200, compact_threshold=0.5, recent_messages=1, enable_llm_summarization=True,
        )
        older = [
            Message(role="user", content="earlier turn one"),
            Message(role="assistant", content="earlier turn two"),
        ]
        summary = await mgr._llm_summarize_messages(older, agent, max_summary_chars=500)
        self.assertEqual(adapter.call_count, 1)
        self.assertIsNotNone(summary)
        self.assertIn("compacted to stay within the model budget", summary.content)


if __name__ == "__main__":
    unittest.main()
