"""read_pdf 内置工具的运行时行为测试。

覆盖：文字/截图双模式、页码选择器、多模态 tool result 透传（模型侧保留图片、
SSE 与压缩路径剥离 base64）、路径穿越与页数上限防护。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pymupdf

import covalent.core.pdf_tools as pdf_tools_module
from covalent.core.agent import AgentSpec
from covalent.core.pdf_tools import PDF_MAX_PAGES_PER_CALL, _parse_page_selector, register_pdf_tools
from covalent.core.types import Message, RunContext, ToolResult
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.context_window_manager import ContextWindowManager
from covalent.runtime.react import ReactAgentRuntime

from tests.helpers import ScriptedModelAdapter, text_response, tool_call_response


class FakeMemoryStore:
    def __init__(self) -> None:
        self.saved: list[list[Message]] = []

    async def load(self, scope_kind: str, scope_id: str | None) -> list[Message]:
        return []

    async def save(self, scope_kind: str, scope_id: str | None, messages: list[Message]) -> None:
        self.saved.append([m.model_copy(deep=True) for m in messages])


class StubSettings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.session_workspace_enabled = True

    def session_workspace_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def workspace_root(self) -> Path:
        return self.root


def _make_pdf(path: Path, page_texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_textbox(page.rect, text)
    document.save(path)
    document.close()


class ParsePageSelectorTests(unittest.TestCase):
    def test_selector_variants(self) -> None:
        self.assertEqual(_parse_page_selector("1,3,5-8", 10), [1, 3, 5, 6, 7, 8])
        self.assertEqual(_parse_page_selector("all", 3), [1, 2, 3])
        self.assertEqual(_parse_page_selector("3", 5), [3])
        self.assertEqual(_parse_page_selector("", 2), [1])
        self.assertEqual(_parse_page_selector("2,2,1", 5), [1, 2])
        with self.assertRaisesRegex(ValueError, "out of range"):
            _parse_page_selector("9", 3)
        with self.assertRaisesRegex(ValueError, "Invalid page selector"):
            _parse_page_selector("abc", 3)
        with self.assertRaisesRegex(ValueError, "at most"):
            _parse_page_selector("1-10", 10)
        self.assertEqual(len(_parse_page_selector("1-9", 10)), PDF_MAX_PAGES_PER_CALL)


class ReadPdfToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name) / "ws"
        self.settings = StubSettings(self.workspace)

    def _build_runtime(self, responses: list) -> tuple[ReactAgentRuntime, FakeMemoryStore, ScriptedModelAdapter]:
        adapter = ScriptedModelAdapter(responses)
        registry = FrameworkRegistry()
        registry.register_agent(
            AgentSpec(
                name="main",
                description="reader",
                system_prompt="prompt",
                provider=adapter.config,
                local_tools=["read_pdf"],
            )
        )
        registry.model_providers[adapter.config.cache_key()] = adapter
        register_pdf_tools(registry, self.settings)
        memory = FakeMemoryStore()
        runtime = ReactAgentRuntime(registry=registry, memory_store=memory, enable_llm_summarization=False)
        return runtime, memory, adapter

    def _seed_script(self, runtime: ReactAgentRuntime, arguments: dict[str, Any]) -> ScriptedModelAdapter:
        agent = runtime.registry.agents["main"]
        adapter = runtime.registry.model_providers[agent.provider.cache_key()]
        assert isinstance(adapter, ScriptedModelAdapter)
        adapter._responses[:] = [tool_call_response("read_pdf", arguments=arguments), text_response("done")]
        adapter._index = 0
        return adapter

    async def _run(self, runtime: ReactAgentRuntime, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        agent = runtime.registry.agents["main"]
        context = RunContext(agent_name="main", session_id="sess-1")
        self._seed_script(runtime, arguments)
        events: list[dict[str, Any]] = []
        async for event in runtime.stream_events(agent, "read the pdf", context):
            events.append(event)
        return events

    def _tool_result_payloads(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for event in events:
            if event.get("event") == "tool_results":
                results.extend(event["payload"]["results"])
        return results

    def _pdf_arguments(self, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {"path": "uploads/doc.pdf"}
        arguments.update(overrides)
        return arguments

    async def test_read_pdf_text_mode(self) -> None:
        _make_pdf(self.workspace / "sess-1" / "uploads" / "doc.pdf", ["alpha page", "", "gamma page"])
        runtime, _memory, adapter = self._build_runtime([])

        events = await self._run(runtime, self._pdf_arguments(pages="1,3", mode="text"))

        result = self._tool_result_payloads(events)[0]
        self.assertFalse(result["is_error"])
        payload = json.loads(result["content"])
        self.assertEqual(payload["pages"], [1, 3])
        self.assertEqual(payload["page_count"], 3)
        self.assertIn("[Page 1]\nalpha page", payload["content"])
        self.assertIn("[Page 3]\ngamma page", payload["content"])
        self.assertFalse(payload["truncated"])
        # 第二次生成请求里 tool 消息内容是 JSON 字符串（text 模式无图片）。
        tool_messages = [m for m in adapter.received_requests[1].messages if m.role == "tool"]
        self.assertTrue(tool_messages)
        self.assertIsInstance(tool_messages[0].content, str)

    async def test_read_pdf_text_mode_marks_truncation(self) -> None:
        _make_pdf(self.workspace / "sess-1" / "uploads" / "doc.pdf", ["word " * 200 for _ in range(3)])
        original = pdf_tools_module.PDF_MAX_TEXT_CHARS_PER_CALL
        pdf_tools_module.PDF_MAX_TEXT_CHARS_PER_CALL = 500
        self.addCleanup(setattr, pdf_tools_module, "PDF_MAX_TEXT_CHARS_PER_CALL", original)
        runtime, _memory, _adapter = self._build_runtime([])

        events = await self._run(runtime, self._pdf_arguments(pages="1-3", mode="text"))

        result = self._tool_result_payloads(events)[0]
        payload = json.loads(result["content"])
        self.assertTrue(payload["truncated"])
        self.assertIn("request fewer pages", payload["note"])

    async def test_read_pdf_image_mode_delivers_images_to_model_only(self) -> None:
        _make_pdf(self.workspace / "sess-1" / "uploads" / "doc.pdf", ["one", "two"])
        runtime, memory, adapter = self._build_runtime([])

        events = await self._run(runtime, self._pdf_arguments(pages="1-2", mode="image"))

        # 模型侧：第二次生成请求的 tool 消息是 content-part list，含 2 个图片块。
        tool_messages = [m for m in adapter.received_requests[1].messages if m.role == "tool"]
        self.assertTrue(tool_messages)
        content = tool_messages[0].content
        self.assertIsInstance(content, list)
        image_parts = [item for item in content if item.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 2)
        for part in image_parts:
            self.assertTrue(str(part["image_url"]["url"]).startswith("data:image/png;base64,"))
        self.assertIn("rendered as images", content[0]["text"])

        # SSE/事件侧：任何事件不得携带 base64。
        dumped = json.dumps(events)
        self.assertNotIn("data:image/png;base64,", dumped)
        event_results = self._tool_result_payloads(events)
        summary = event_results[0]["content"]
        self.assertIn("[2 images omitted]", summary)

        # 持久化侧：memory 中 tool 消息保留 list，model_dump/model_validate round-trip 不丢块。
        saved_messages = memory.saved[-1]
        persisted_tool = [m for m in saved_messages if m.role == "tool"][0]
        self.assertIsInstance(persisted_tool.content, list)
        round_tripped = Message.model_validate(persisted_tool.model_dump(mode="json"))
        self.assertEqual(round_tripped.content, persisted_tool.content)

    def test_tool_result_to_message_passthrough(self) -> None:
        content_list = [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        message = ToolResult(name="read_pdf", content=content_list).to_message()
        self.assertEqual(message.role, "tool")
        self.assertEqual(message.content, content_list)

        dict_content = {"status": "ok", "path": "x"}
        message = ToolResult(name="publish", content=dict_content).to_message()
        self.assertIsInstance(message.content, str)

    def test_compaction_weights_and_flattens_images(self) -> None:
        runtime, _memory, _adapter = self._build_runtime([])
        manager = ContextWindowManager(
            runtime,
            session_history_limit=100,
            context_compact_threshold=0.8,
            context_recent_messages=10,
            context_summary_char_budget=6000,
            context_message_char_limit=20000,
            context_min_recent_messages=1,
            context_summary_model=None,
            enable_llm_summarization=False,
        )
        content = [
            {"type": "text", "text": "x" * 100},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 2000}},
        ]
        message = Message(role="tool", content=content, tool_call_id="call_1")

        self.assertGreaterEqual(manager._estimate_message_chars(message), 2000)

        summary = manager._summarize_tool_content(content, 120)
        self.assertLessEqual(len(summary), 120)
        self.assertIn("[1 image]", summary)
        self.assertNotIn("AAAA", summary)

    async def test_read_pdf_rejections(self) -> None:
        _make_pdf(self.workspace / "sess-1" / "uploads" / "doc.pdf", ["one", "two"])
        (self.workspace / "sess-1" / "uploads" / "notes.txt").write_text("not a pdf", encoding="utf-8")
        runtime, _memory, _adapter = self._build_runtime([])

        traversal = self._tool_result_payloads(
            await self._run(runtime, self._pdf_arguments(path="../outside.pdf"))
        )[0]
        self.assertTrue(traversal["is_error"])
        self.assertIn("escapes root", traversal["content"])

        not_pdf = self._tool_result_payloads(
            await self._run(runtime, self._pdf_arguments(path="uploads/notes.txt"))
        )[0]
        self.assertTrue(not_pdf["is_error"])
        self.assertIn("Not a PDF file", not_pdf["content"])

        out_of_range = self._tool_result_payloads(
            await self._run(runtime, self._pdf_arguments(pages="9"))
        )[0]
        self.assertTrue(out_of_range["is_error"])
        self.assertIn("out of range", out_of_range["content"])
