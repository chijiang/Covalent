"""Tests for the OpenAI Responses API provider (api_style="responses")."""

from __future__ import annotations

import unittest
from typing import Any

from covalent.core.types import GenerationRequest, Message
from covalent.model.base import ProviderConfig
from covalent.model.factory import build_provider
from covalent.model.openai_responses import ResponsesProvider


class _FakeChunk:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._data


class _FakeResponses:
    def __init__(self, events: list[dict[str, Any]], *, final: _FakeChunk | None = None) -> None:
        self._events = events
        self._final = final
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._final is not None:
            return self._final

        outer = self

        class _Stream:
            def __aiter__(self):
                outer._iter = iter(outer._events)
                return self

            async def __anext__(self):
                try:
                    return _FakeChunk(next(outer._iter))
                except StopIteration:
                    raise StopAsyncIteration from None

        return _Stream()


class _FakeClient:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses


def _provider(
    events: list[dict[str, Any]] | None = None,
    *,
    final: _FakeChunk | None = None,
    config: ProviderConfig | None = None,
) -> tuple[ResponsesProvider, _FakeResponses]:
    config = config or ProviderConfig(
        provider="openai_compatible",
        model="gpt-5",
        api_key="k",
        base_url="http://fake/v1",
        api_style="responses",
    )
    provider = ResponsesProvider(config)
    fake = _FakeResponses(events or [], final=final)
    provider._client = _FakeClient(fake)  # noqa: SLF001 — test seam
    return provider, fake


def _request(**overrides: Any) -> GenerationRequest:
    defaults: dict[str, Any] = {
        "model": "gpt-5",
        "messages": [Message(role="user", content="hi")],
        "tools": [],
    }
    defaults.update(overrides)
    return GenerationRequest(**defaults)


_COMPLETED = {
    "type": "response.completed",
    "response": {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    },
}


class StreamingAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_deltas_usage_and_single_response_tag(self) -> None:
        provider, _fake = _provider([
            {"type": "response.reasoning_summary_text.delta", "delta": "think"},
            {"type": "response.output_text.delta", "delta": "Hello "},
            {"type": "response.output_text.delta", "delta": "world"},
            _COMPLETED,
        ])

        tags: list[str] = []
        response = None
        async for tag, value in provider.stream_generation(_request()):
            tags.append(tag)
            if tag == "delta":
                continue
            if tag == "reasoning":
                self.assertEqual(value, "think")
            else:
                response = value

        self.assertEqual(tags[-1], "response")
        self.assertEqual(tags.count("response"), 1)
        assert response is not None
        self.assertEqual(response.output_text, "Hello world")
        self.assertEqual(response.assistant_message.reasoning_content, "think")
        usage = response.usage
        assert usage is not None
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (10, 4, 14))
        self.assertEqual(usage.cached_tokens, 3)
        self.assertEqual(usage.reasoning_tokens, 2)

    async def test_function_call_aggregated_with_completions_shaped_raw(self) -> None:
        completed = {
            "type": "response.completed",
            "response": {
                "output": [
                    {"type": "function_call", "call_id": "call-9", "name": "get_time", "arguments": "{\"tz\":\"utc\"}"},
                ],
                "usage": {},
            },
        }
        provider, _fake = _provider([completed])

        responses = [value async for tag, value in provider.stream_generation(_request()) if tag == "response"]

        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertEqual(len(response.tool_calls), 1)
        tool_call = response.tool_calls[0]
        self.assertEqual((tool_call.id, tool_call.name), ("call-9", "get_time"))
        self.assertEqual(tool_call.arguments, {"tz": "utc"})
        # raw 必须是 completions 形状，供历史回放与工具名改写消费。
        self.assertEqual(
            tool_call.raw,
            {"id": "call-9", "type": "function", "function": {"name": "get_time", "arguments": "{\"tz\":\"utc\"}"}},
        )
        self.assertEqual(response.assistant_message.tool_calls, [tool_call.raw])

    async def test_failed_event_raises_provider_error(self) -> None:
        provider, _fake = _provider([
            {"type": "response.failed", "response": {"error": {"code": "boom", "message": "kaboom"}}},
        ])
        with self.assertRaises(Exception) as ctx:
            async for _ in provider.stream_generation(_request()):
                pass
        self.assertIn("kaboom", str(ctx.exception))

    async def test_stream_without_completed_event_raises(self) -> None:
        provider, _fake = _provider([
            {"type": "response.output_text.delta", "delta": "partial"},
        ])
        with self.assertRaises(Exception) as ctx:
            async for _ in provider.stream_generation(_request()):
                pass
        self.assertIn("completed", str(ctx.exception))


class RequestMappingTests(unittest.IsolatedAsyncioTestCase):
    async def _captured_request(self, request: GenerationRequest) -> dict[str, Any]:
        provider, fake = _provider([_COMPLETED])
        async for _tag, _value in provider.stream_generation(request):
            pass
        return fake.calls[0]

    async def test_full_mapping(self) -> None:
        request = _request(
            system_prompt="You are terse.",
            messages=[
                Message(role="system", content="extra system"),
                Message(role="user", content="look", ),
                Message(role="user", content=[
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                    {"type": "unknown_part", "foo": 1},
                ]),
                Message(
                    role="assistant",
                    content="calling tool",
                    tool_calls=[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}],
                ),
                Message(role="tool", tool_call_id="c1", content="tool says hi"),
            ],
            tools=[{
                "type": "function",
                "function": {"name": "f", "description": "does f", "parameters": {"type": "object", "properties": {}}},
            }],
            reasoning_level="high",
            max_tokens=256,
        )

        payload = await self._captured_request(request)

        self.assertEqual(payload["instructions"], "You are terse.")
        self.assertNotIn("stream_options", payload)
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 256)
        self.assertEqual(payload["tools"], [{
            "type": "function", "name": "f", "description": "does f",
            "parameters": {"type": "object", "properties": {}},
        }])
        items = payload["input"]
        self.assertEqual(items[0]["role"], "system")
        self.assertEqual(items[1]["content"], [{"type": "input_text", "text": "look"}])
        image_items = items[2]["content"]
        self.assertEqual(image_items[0], {"type": "input_text", "text": "what is this"})
        self.assertEqual(image_items[1], {"type": "input_image", "image_url": "data:image/png;base64,xxx"})
        self.assertEqual(len(image_items), 2, "unsupported part types must be dropped")
        self.assertEqual(items[3]["type"], "message")
        self.assertEqual(items[4], {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{\"a\":1}"})
        self.assertEqual(items[5], {"type": "function_call_output", "call_id": "c1", "output": "tool says hi"})

    async def test_reasoning_levels(self) -> None:
        self.assertIsNone(ResponsesProvider._reasoning_effort("none"))
        self.assertIsNone(ResponsesProvider._reasoning_effort(""))
        self.assertEqual(ResponsesProvider._reasoning_effort("low"), "low")
        self.assertEqual(ResponsesProvider._reasoning_effort("minimal"), "minimal")
        self.assertEqual(ResponsesProvider._reasoning_effort("xhigh"), "high")
        self.assertEqual(ResponsesProvider._reasoning_effort("max"), "high")
        self.assertEqual(ResponsesProvider._reasoning_effort("banana"), "high")


class FactoryAndCacheKeyTests(unittest.TestCase):
    def test_factory_dispatches_on_api_style(self) -> None:
        responses_config = ProviderConfig(
            provider="openai_compatible", model="m", api_key="k", base_url="http://x/v1", api_style="responses"
        )
        self.assertIsInstance(build_provider(responses_config), ResponsesProvider)
        completions_config = responses_config.model_copy(update={"api_style": None})
        from covalent.model.openai_compatible import OpenAICompatibleProvider
        self.assertIsInstance(build_provider(completions_config), OpenAICompatibleProvider)

    def test_factory_rejects_apih_with_responses(self) -> None:
        config = ProviderConfig(provider="apih", model="m", api_key="k", base_url="http://x", api_style="responses")
        with self.assertRaises(ValueError):
            build_provider(config)

    def test_api_style_changes_cache_key(self) -> None:
        base = ProviderConfig(provider="openai_compatible", model="m", base_url="http://x/v1")
        with_responses = base.model_copy(update={"api_style": "responses"})
        self.assertNotEqual(base.cache_key(), with_responses.cache_key())


class MergeProviderConfigTests(unittest.TestCase):
    def test_inherits_api_style_from_default_provider(self) -> None:
        from covalent.application.services.management_service import _merge_provider_config

        default = ProviderConfig(
            provider="openai_compatible", model="m1", base_url="http://x/v1", api_style="responses", api_key="k"
        )
        # Agent 级未覆盖 endpoint：应继承默认连接的 api_style。
        merged = _merge_provider_config(
            ProviderConfig(provider="openai_compatible", model="m2", base_url=""), default
        )
        self.assertEqual(merged.api_style, "responses")
        # Agent 级显式换 endpoint 且未声明 style：保持 chat completions。
        merged_explicit = _merge_provider_config(
            ProviderConfig(provider="openai_compatible", model="m2", base_url="http://other/v1"), default
        )
        self.assertIsNone(merged_explicit.api_style)
        # Agent 级自带 style（selected_provider 携带行的 api_style）优先。
        merged_own = _merge_provider_config(
            ProviderConfig(
                provider="openai_compatible", model="m2", base_url="http://other/v1", api_style="responses"
            ),
            default,
        )
        self.assertEqual(merged_own.api_style, "responses")


if __name__ == "__main__":
    unittest.main()
