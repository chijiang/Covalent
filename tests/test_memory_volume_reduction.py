"""Unit tests for two memory-volume reductions:

- A: `_strip_tool_result_images` replaces base64 image parts in TOOL messages
  with a text note when persisting session memory (in-run messages untouched).
- D: `_compact_old_tool_results` (Tier 0) head-biased summarization of tool
  results beyond the most recent few, before the global budget gate.
"""

from __future__ import annotations

import unittest
from typing import Any

from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationResponse, Message
from covalent.model.base import ProviderConfig
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.context_window_manager import (
    OLD_TOOL_RESULT_CHAR_LIMIT,
    RECENT_TOOL_RESULTS_KEPT,
    ContextWindowManager,
)

from tests.helpers import ScriptedModelAdapter


def _manager(**overrides: Any) -> tuple[ContextWindowManager, AgentSpec]:
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

    registry = FrameworkRegistry()
    agent = AgentSpec(
        name="test",
        description="d",
        system_prompt="s",
        provider=ProviderConfig(provider="test", model="test-model"),
        context_window=overrides.pop("token_budget", 1_000_000),
    )
    registry.register_agent(agent)
    registry.model_providers[agent.provider.cache_key()] = ScriptedModelAdapter(
        [GenerationResponse(model="test-model", output_text="summary")]
    )
    stub = _RuntimeStub()
    stub.registry = registry  # type: ignore[attr-defined]
    kwargs = dict(
        session_history_limit=1000,
        context_compact_threshold=0.75,
        context_recent_messages=12,
        context_summary_char_budget=2000,
        context_message_char_limit=100_000,
        context_min_recent_messages=1,
        context_summary_model=None,
        enable_llm_summarization=True,
    )
    kwargs.update(overrides)
    mgr = ContextWindowManager(stub, **kwargs)  # type: ignore[arg-type]
    return mgr, agent


def _image_tool_message(name: str = "read_pdf", url: str = "data:image/png;base64," + "A" * 500) -> Message:
    return Message(
        role="tool",
        name=name,
        tool_call_id="call-1",
        content=[
            {"type": "text", "text": f"{name} result"},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    )


class StripToolResultImagesTests(unittest.TestCase):
    def test_tool_images_replaced_with_note(self) -> None:
        mgr, _ = _manager()
        messages = [
            Message(role="user", content="read the pdf"),
            Message(role="assistant", content="", tool_calls=[{"id": "call-1", "function": {"name": "read_pdf", "arguments": "{}"}}]),
            _image_tool_message(),
        ]
        prepared = mgr._strip_tool_result_images(messages)
        tool = prepared[2]
        self.assertNotIn("image_url", [item.get("type") for item in tool.content if isinstance(item, dict)])
        self.assertEqual(len(tool.content), 2)
        note = tool.content[1]
        self.assertIn("1 image omitted from memory", note["text"])
        self.assertIn("re-run the tool", note["text"])
        # original untouched
        self.assertEqual(messages[2].content[1]["type"], "image_url")

    def test_multiple_images_counted(self) -> None:
        mgr, _ = _manager()
        message = Message(
            role="tool",
            name="read_pdf",
            tool_call_id="c",
            content=[
                {"type": "text", "text": "pages"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,y"}},
            ],
        )
        prepared = mgr._strip_tool_result_images([message])
        self.assertIn("2 images omitted from memory", prepared[0].content[-1]["text"])

    def test_user_and_assistant_images_untouched(self) -> None:
        mgr, _ = _manager()
        messages = [
            Message(role="user", content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]),
            Message(role="assistant", content="done"),
        ]
        prepared = mgr._strip_tool_result_images(messages)
        self.assertEqual(prepared[0].content[0]["type"], "image_url")

    def test_non_image_tool_messages_untouched(self) -> None:
        mgr, _ = _manager()
        message = Message(role="tool", name="run_shell", tool_call_id="c", content="plain text result")
        prepared = mgr._strip_tool_result_images([message])
        self.assertEqual(prepared[0].content, "plain text result")

    def test_no_tool_images_returns_same_list(self) -> None:
        mgr, _ = _manager()
        messages = [Message(role="user", content="hi")]
        self.assertIs(mgr._strip_tool_result_images(messages), messages)


class CompactOldToolResultsTests(unittest.TestCase):
    def _conversation(self, tool_results: list[str]) -> list[Message]:
        messages: list[Message] = []
        for index, result in enumerate(tool_results):
            messages.append(Message(role="user", content=f"q{index}"))
            messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[{"id": f"call-{index}", "function": {"name": "t", "arguments": "{}"}}],
                )
            )
            messages.append(Message(role="tool", name="t", tool_call_id=f"call-{index}", content=result))
        return messages

    def test_recent_tools_verbatim_old_summarized(self) -> None:
        mgr, _ = _manager()
        big = "x" * (OLD_TOOL_RESULT_CHAR_LIMIT * 3)
        results = [big, big, big, big, big]
        messages = self._conversation(results)
        prepared, count = mgr._compact_old_tool_results(messages)
        self.assertEqual(count, len(results) - RECENT_TOOL_RESULTS_KEPT)
        for offset in range(RECENT_TOOL_RESULTS_KEPT):
            index = len(prepared) - 1 - offset * 3
            self.assertEqual(prepared[index].content, big)
        old = prepared[2]
        self.assertLessEqual(len(str(old.content)), OLD_TOOL_RESULT_CHAR_LIMIT + 200)

    def test_small_old_results_untouched(self) -> None:
        mgr, _ = _manager()
        messages = self._conversation(["ok", "ok", "ok", "ok", "ok"])
        prepared, count = mgr._compact_old_tool_results(messages)
        self.assertEqual(count, 0)
        self.assertIs(prepared, messages)

    def test_gate_reports_old_tool_prune_without_compaction(self) -> None:
        mgr, agent = _manager(token_budget=10_000_000)
        big = "x" * 200_000
        messages = self._conversation([big] * 8)
        import asyncio

        async def run():
            return await mgr._compact_generation_messages(messages, agent=agent, last_prompt_tokens=None)

        result, stats = asyncio.run(run())
        self.assertFalse(stats["compacted"], "tier 0 alone should not mark compacted=True when under budget")
        self.assertEqual(stats["compaction_method"], "old-tool-prune")
        self.assertEqual(stats["old_tool_results_compacted"], 8 - RECENT_TOOL_RESULTS_KEPT)
        self.assertLess(stats["request_char_count"], 8 * 200_000)
        # oldest four tool messages summarized; recent four verbatim
        self.assertLess(len(str(result[2].content)), 10_000)
        self.assertEqual(len(str(result[-1].content)), 200_000)


if __name__ == "__main__":
    unittest.main()
