"""猜你想问（suggested_questions）链路的运行时行为测试。

覆盖：<suggested_questions> 尾标签的流式状态机（含跨 chunk 撕裂）、聚合权威
剥离与问题解析、capability 门控的 policy 注入、非流式路径、delegate 转发兜底，
以及 final payload 的 suggestions 回填与落库 transcript 清理。

运行：PYTHONPATH=src uv run pytest tests/test_suggested_questions.py -q
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from covalent.core.agent import AgentSpec
from covalent.core.types import (
    Capability,
    GenerationResponse,
    Message,
    RunContext,
)
from covalent.model.base import ModelAdapter, ProviderConfig
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.react import (
    SUGGESTED_QUESTIONS_POLICY,
    ReactAgentRuntime,
    SuggestedQuestionsSplitter,
    strip_suggested_questions,
)

VISIBLE = "答案正文"
SUGGESTIONS = ["追问纬度", "追问趋势", "追问来源"]
TAGGED_JSON = json.dumps(SUGGESTIONS, ensure_ascii=False)
TAG = f"<suggested_questions>{TAGGED_JSON}</suggested_questions>"


# -- SuggestedQuestionsSplitter 单元 ------------------------------------------


def test_splitter_no_tag_passthrough():
    splitter = SuggestedQuestionsSplitter()
    assert splitter.feed("普通文本") == ("普通文本", "")
    assert splitter.flush() == ("", "")


def test_splitter_strips_tag_and_swallows_tail_after_close():
    splitter = SuggestedQuestionsSplitter()
    visible, captured = splitter.feed(f"{VISIBLE}{TAG}闭标签后的残余应该被丢弃")
    assert visible == VISIBLE
    assert splitter.flush() == ("", TAGGED_JSON)


def test_splitter_tag_split_across_chunks():
    splitter = SuggestedQuestionsSplitter()
    visible_parts: list[str] = []
    captured_parts: list[str] = []
    for chunk in (
        VISIBLE[:2],
        VISIBLE[2:],
        "<suggested_q",
        "uestions>" + TAGGED_JSON[:10],
        TAGGED_JSON[10:],
        "</suggest",
        "ed_questions>",
        "尾部",
    ):
        visible, captured = splitter.feed(chunk)
        visible_parts.append(visible)
        captured_parts.append(captured)
    tail, _captured = splitter.flush()
    visible_parts.append(tail)
    # feed 出口是流式增量视图，flush 出口是完整体，二者择一累加即可。
    assert "".join(visible_parts) == VISIBLE
    assert "".join(captured_parts) == TAGGED_JSON


def test_splitter_flush_returns_full_captured_body():
    splitter = SuggestedQuestionsSplitter()
    for chunk in (VISIBLE[:2], VISIBLE[2:], "<suggested_q"):
        splitter.feed(chunk)
    for chunk in ("uestions>", TAGGED_JSON, "</suggested_questions>", "残留"):
        splitter.feed(chunk)
    assert splitter.flush() == ("", TAGGED_JSON)


def test_splitter_unclosed_tag_drops_captured_tail():
    # 流结束仍未闭合：捕获体整体丢弃，部分 JSON 不外泄为可见文本。
    splitter = SuggestedQuestionsSplitter()
    visible, _captured = splitter.feed(VISIBLE + "<suggested_questions>\"残缺")
    assert visible == VISIBLE
    assert splitter.flush() == ("", "")


def test_splitter_swallow_mode_drops_everything_between_tags():
    splitter = SuggestedQuestionsSplitter()
    visible, _ = splitter.feed(VISIBLE + "<suggested_questions>")
    visible2, _ = splitter.feed("<suggested_questions>内嵌标签不该触发")
    assert visible == VISIBLE and visible2 == ""
    assert splitter.flush() == ("", "")


# -- strip_suggested_questions 聚合剥离 --------------------------------------


def test_strip_no_tag_unchanged():
    assert strip_suggested_questions(VISIBLE) == (VISIBLE, [])


def test_strip_parses_and_removes_tag():
    cleaned, suggestions = strip_suggested_questions(f"{VISIBLE}\n{TAG}")
    assert cleaned == VISIBLE
    assert suggestions == SUGGESTIONS


def test_strip_drops_text_after_close_tag():
    cleaned, suggestions = strip_suggested_questions(f"{VISIBLE}{TAG}多余尾部")
    assert cleaned == VISIBLE
    assert suggestions == SUGGESTIONS


def test_strip_unclosed_tag_drops_everything_from_open():
    cleaned, suggestions = strip_suggested_questions(f"{VISIBLE}<suggested_questions>\"残缺")
    assert cleaned == VISIBLE
    assert suggestions == []


def test_strip_invalid_json_or_items_drops_suggestions():
    cleaned, suggestions = strip_suggested_questions(
        f"{VISIBLE}<suggested_questions>不是JSON</suggested_questions>"
    )
    assert cleaned == VISIBLE and suggestions == []
    cleaned2, suggestions2 = strip_suggested_questions(
        f"{VISIBLE}<suggested_questions>[42, \"合法\"]</suggested_questions>"
    )
    assert cleaned2 == VISIBLE
    assert suggestions2 == ["合法"]


def test_strip_caps_at_three():
    many = json.dumps([f"问题{i}" for i in range(5)], ensure_ascii=False)
    _cleaned, suggestions = strip_suggested_questions(
        f"{VISIBLE}<suggested_questions>{many}</suggested_questions>"
    )
    assert len(suggestions) == 3


# -- 运行时：capability 门控与流式/非流式剥离 --------------------------------


class StreamingScriptedAdapter(ModelAdapter):
    def __init__(self, scripts: list[list[tuple[str, Any]]], *, streaming: bool = True) -> None:
        super().__init__(ProviderConfig(provider="fake", model="fake-model"))
        self.scripts = [list(script) for script in scripts]
        self.streaming = streaming
        self.calls = 0
        self.requests: list[Any] = []

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.STREAMING} if self.streaming else set()

    async def stream_generation(self, request):
        self.calls += 1
        self.requests.append(request)
        for item in self.scripts.pop(0):
            yield item

    async def generate(self, request) -> GenerationResponse:
        self.calls += 1
        self.requests.append(request)
        for item in self.scripts.pop(0):
            if item[0] == "response":
                return item[1]
        raise AssertionError("非流式脚本必须包含 ('response', ...) 项")


class FakeMemoryStore:
    def __init__(self) -> None:
        self.saved: list[list[Message]] = []

    async def load(self, scope_kind: str, scope_id: str | None) -> list[Message]:
        return []

    async def save(self, scope_kind: str, scope_id: str | None, messages: list[Message]) -> None:
        self.saved.append([m.model_copy(deep=True) for m in messages])


def _runtime(
    scripts: list[list[tuple[str, Any]]],
    *,
    streaming: bool = True,
    capabilities: set[Capability] | None = None,
) -> tuple[ReactAgentRuntime, FakeMemoryStore, StreamingScriptedAdapter]:
    adapter = StreamingScriptedAdapter(scripts, streaming=streaming)
    registry = FrameworkRegistry()
    registry.model_providers[adapter.config.cache_key()] = adapter
    registry.register_agent(
        AgentSpec(
            name="main",
            description="测试 agent",
            system_prompt="测试提示词",
            provider=adapter.config,
            capabilities=capabilities or {Capability.CHAT, Capability.REACT},
        )
    )
    memory = FakeMemoryStore()
    runtime = ReactAgentRuntime(registry=registry, session_store=None, memory_store=memory)
    return runtime, memory, adapter


async def _collect(runtime: ReactAgentRuntime) -> list[dict[str, Any]]:
    agent = runtime.registry.agents["main"]
    context = RunContext(agent_name="main", session_id="sess-1")
    events: list[dict[str, Any]] = []
    async for event in runtime.stream_events(agent, "问题", context):
        events.append(event)
    return events


def _texts(events: list[dict[str, Any]], name: str) -> str:
    return "".join(
        event["payload"].get("text", "") for event in events if event.get("event") == name
    )


def _final_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["payload"] for e in events if e.get("event") == "final"]


@pytest.mark.asyncio
async def test_streaming_suggestions_stripped_and_carried_in_final():
    prefix = f"{VISIBLE}\n"
    final = GenerationResponse(
        output_text=f"{prefix}{TAG}",
        tool_calls=[],
        assistant_message=Message(role="assistant", content=f"{prefix}{TAG}"),
    )
    runtime, memory, adapter = _runtime(
        [
            [
                ("delta", VISIBLE),
                ("delta", "\n<suggested_q"),
                ("delta", "uestions>" + TAGGED_JSON[:8]),
                ("delta", TAGGED_JSON[8:] + "</suggested_questions>"),
                ("response", final),
            ]
        ],
        capabilities={Capability.CHAT, Capability.REACT, Capability.SUGGESTED_QUESTIONS},
    )

    events = await _collect(runtime)

    # 增量流不得泄露标签；答案与标签之间的换行是合法可见内容。
    deltas = _texts(events, "assistant_delta")
    assert deltas == prefix
    assert "suggested_questions" not in deltas
    final_payload = _final_payloads(events)[0]
    assert final_payload["output_text"] == VISIBLE
    assert final_payload["suggestions"] == SUGGESTIONS
    # policy 注入 + 落库 transcript 干净。
    assert SUGGESTED_QUESTIONS_POLICY in (adapter.requests[0].system_prompt or "")
    saved_assistant = [m for m in memory.saved[-1] if m.role == "assistant"][-1]
    assert saved_assistant.content == VISIBLE
    assert "suggested_questions" not in saved_assistant.content


@pytest.mark.asyncio
async def test_non_streaming_suggestions_stripped_and_carried():
    final = GenerationResponse(
        output_text=f"{VISIBLE}\n{TAG}",
        tool_calls=[],
        assistant_message=Message(role="assistant", content=f"{VISIBLE}\n{TAG}"),
    )
    runtime, _memory, adapter = _runtime(
        [[("response", final)]],
        streaming=False,
        capabilities={Capability.CHAT, Capability.SUGGESTED_QUESTIONS},
    )

    events = await _collect(runtime)

    final_payload = _final_payloads(events)[0]
    assert final_payload["output_text"] == VISIBLE
    assert final_payload["suggestions"] == SUGGESTIONS
    assert SUGGESTED_QUESTIONS_POLICY in (adapter.requests[0].system_prompt or "")


@pytest.mark.asyncio
async def test_capability_off_keeps_text_and_no_policy():
    final = GenerationResponse(
        output_text=VISIBLE,
        tool_calls=[],
        assistant_message=Message(role="assistant", content=VISIBLE),
    )
    runtime, _memory, adapter = _runtime(
        [[("delta", VISIBLE), ("response", final)]],
        capabilities={Capability.CHAT, Capability.REACT},
    )

    events = await _collect(runtime)

    assert _texts(events, "assistant_delta") == VISIBLE
    final_payload = _final_payloads(events)[0]
    assert final_payload["output_text"] == VISIBLE
    assert final_payload.get("suggestions") in (None, [])
    assert SUGGESTED_QUESTIONS_POLICY not in (adapter.requests[0].system_prompt or "")


@pytest.mark.asyncio
async def test_tag_misbehaving_with_capability_off_still_cleaned_in_strip():
    # 防御式剥离无条件生效：即便模型未开启能力却输出了标签，聚合 final 与
    # 落库 transcript 依然干净（流式 delta 无 splitter，短暂可见属预期边界）。
    final = GenerationResponse(
        output_text=f"{VISIBLE}\n{TAG}",
        tool_calls=[],
        assistant_message=Message(role="assistant", content=f"{VISIBLE}\n{TAG}"),
    )
    runtime, memory, _adapter = _runtime(
        [[("response", final)]],
        streaming=False,
        capabilities={Capability.CHAT, Capability.REACT},
    )

    events = await _collect(runtime)

    final_payload = _final_payloads(events)[0]
    assert final_payload["output_text"] == VISIBLE
    assert final_payload["suggestions"] == SUGGESTIONS
    saved_assistant = [m for m in memory.saved[-1] if m.role == "assistant"][-1]
    assert "suggested_questions" not in saved_assistant.content