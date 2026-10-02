import json

import httpx
import pytest

from covalent_runtime.domain.types import GenerationRequest
from covalent_runtime.ports.model import ModelProviderError, ProviderConfig
from covalent_agent_kit.models.anthropic_compatible import AnthropicCompatibleProvider, derive_anthropic_base_url
from covalent_agent_kit.models.factory import build_provider


def make_provider(monkeypatch, handler, **config_updates):
    clients = []
    def factory(self, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(AnthropicCompatibleProvider, "_http_client", factory)
    provider = build_provider(ProviderConfig(
        provider="anthropic_compatible",
        model="claude-test",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-secret",
        api_style="messages",
        **config_updates,
    ))
    return provider, clients


def sse(events):
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


@pytest.mark.parametrize("url,expected", [
    ("https://api.anthropic.com", "https://api.anthropic.com/v1"),
    ("https://api.anthropic.com/", "https://api.anthropic.com/v1"),
    ("https://gateway.example/v1", "https://gateway.example/v1"),
    ("https://gateway.example/v1/messages", "https://gateway.example/v1"),
    ("https://gateway.example/messages", "https://gateway.example/v1"),
])
def test_derive_anthropic_base_url(url, expected):
    assert derive_anthropic_base_url(url) == expected


def test_factory_dispatch_and_contract_style():
    config = ProviderConfig(provider="anthropic_compatible", model="claude-test", base_url="https://api.anthropic.com", api_style="messages")
    assert isinstance(build_provider(config), AnthropicCompatibleProvider)


@pytest.mark.asyncio
async def test_generate_request_and_response_mapping(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "msg_1",
            "model": "claude-test",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "answer"},
                {"type": "thinking", "thinking": "because"},
                {"type": "tool_use", "id": "toolu_1", "name": "query", "input": {"x": 1}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 5},
        })
    provider, clients = make_provider(monkeypatch, handler)
    try:
        response = await provider.generate(GenerationRequest(
            model="claude-test",
            messages=[{"role": "user", "content": "question"}],
            system_prompt="be brief",
            tools=[{"type": "function", "function": {"name": "query", "description": "runs", "parameters": {"type": "object", "properties": {}}}}],
        ))
    finally:
        await provider.aclose()
    assert all(client.is_closed for client in clients)

    request = requests[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-test"
    assert body["system"] == "be brief"
    assert body["max_tokens"] == 4096
    assert body["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    assert body["tools"] == [{"name": "query", "description": "runs", "input_schema": {"type": "object", "properties": {}}}]

    assert response.output_text == "answer"
    assert response.assistant_message.reasoning_content == "because"
    [tool_call] = response.tool_calls
    assert tool_call.id == "toolu_1" and tool_call.name == "query" and tool_call.arguments == {"x": 1}
    assert tool_call.raw == {"id": "toolu_1", "type": "function", "function": {"name": "query", "arguments": '{"x": 1}'}}
    assert response.assistant_message.tool_calls == [tool_call.raw]
    assert response.usage.prompt_tokens == 8
    assert response.usage.completion_tokens == 2
    assert response.usage.total_tokens == 10
    assert response.usage.cached_tokens == 5
    assert response.raw_response["id"] == "msg_1"


@pytest.mark.asyncio
async def test_history_replay_mapping(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})
    provider, _ = make_provider(monkeypatch, handler)
    try:
        await provider.generate(GenerationRequest(
            model="claude-test",
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "let me check", "tool_calls": [
                    {"id": "toolu_1", "type": "function", "function": {"name": "query", "arguments": '{"x": 1}'}},
                    {"id": "toolu_2", "type": "function", "function": {"name": "other", "arguments": ""}},
                ]},
                {"role": "tool", "content": '{"result": 42}', "tool_call_id": "toolu_1"},
                {"role": "tool", "content": "done", "tool_call_id": "toolu_2"},
                {"role": "tool", "content": "orphan", "tool_call_id": ""},
                {"role": "user", "content": [
                    {"type": "text", "text": "thanks"},
                    {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
                    {"type": "unknown_thing", "x": 1},
                ]},
            ],
            reasoning_level="high",
        ))
    finally:
        await provider.aclose()
    body = json.loads(requests[0].content)
    assert body["system"] == "be brief"
    # thinking omitted when history contains assistant tool calls; temperature stays.
    assert "thinking" not in body
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 4096
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "question"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "toolu_1", "name": "query", "input": {"x": 1}},
            {"type": "tool_use", "id": "toolu_2", "name": "other", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"result": 42}'},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "done"},
        ]},
        {"role": "user", "content": [
            {"type": "text", "text": "thanks"},
            {"type": "image", "source": {"type": "url", "url": "https://x/img.png"}},
        ]},
    ]


@pytest.mark.asyncio
async def test_thinking_budget_and_max_tokens(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}})
    provider, _ = make_provider(monkeypatch, handler)
    try:
        await provider.generate(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}], reasoning_level="high", max_tokens=9000))
        await provider.generate(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}], reasoning_level="medium"))
    finally:
        await provider.aclose()
    first = json.loads(requests[0].content)
    assert first["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert "temperature" not in first
    assert first["max_tokens"] == 9000
    second = json.loads(requests[1].content)
    assert second["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert second["max_tokens"] == 4096 + 1024


@pytest.mark.asyncio
async def test_stream_generation_events_tools_and_usage(monkeypatch):
    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return sse([
            {"type": "message_start", "message": {"id": "msg_1", "model": "claude-test", "usage": {"input_tokens": 3}}},
            {"type": "ping"},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "query"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"x":'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '1}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ])
    provider, _ = make_provider(monkeypatch, handler)
    try:
        events = [event async for event in provider.stream_generation(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))]
    finally:
        await provider.aclose()
    assert ("delta", "hello") in events
    assert ("reasoning", "hmm") in events
    finals = [value for kind, value in events if kind == "response"]
    assert len(finals) == 1
    final = finals[0]
    [tool_call] = final.tool_calls
    assert tool_call.arguments == {"x": 1}
    assert tool_call.raw == {"id": "toolu_1", "type": "function", "function": {"name": "query", "arguments": '{"x":1}'}}
    assert final.assistant_message.reasoning_content == "hmm"
    assert final.usage.total_tokens == 10
    assert final.raw_response["stop_reason"] == "tool_use"
    assert final.raw_response["id"] == "msg_1"


@pytest.mark.asyncio
async def test_stream_error_event_and_http_error(monkeypatch):
    modes = ["stream_error", "http_error"]
    def handler(request):
        if modes.pop(0) == "stream_error":
            return sse([
                {"type": "message_start", "message": {"id": "msg_1", "usage": {}}},
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            ])
        return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    provider, _ = make_provider(monkeypatch, handler)
    try:
        with pytest.raises(ModelProviderError) as stream_error:
            [event async for event in provider.stream_generation(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))]
        assert stream_error.value.status_code == 502
        assert "Overloaded" in stream_error.value.detail

        with pytest.raises(ModelProviderError) as http_error:
            await provider.generate(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))
        assert http_error.value.status_code == 401
        assert http_error.value.detail == "invalid x-api-key"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_and_empty_stream(monkeypatch):
    def handler(request):
        if modes.pop() == "timeout":
            raise httpx.ConnectTimeout("too slow")
        return httpx.Response(200, text=": keepalive\n\n", headers={"Content-Type": "text/event-stream"})
    modes = ["timeout"]
    provider, _ = make_provider(monkeypatch, handler)
    try:
        with pytest.raises(ModelProviderError) as timeout_error:
            await provider.generate(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))
        assert timeout_error.value.status_code == 504

        modes = ["empty"]
        with pytest.raises(ModelProviderError) as empty_error:
            [event async for event in provider.stream_generation(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))]
        assert empty_error.value.status_code == 502
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_list_models_catalog(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "claude-b"}, {"id": "claude-a"}, {"id": ""}, "junk"]})
    provider, _ = make_provider(monkeypatch, handler)
    try:
        assert await provider.list_models() == ["claude-a", "claude-b"]
    finally:
        await provider.aclose()
    assert requests[0].url.path == "/v1/models"
    assert requests[0].headers["x-api-key"] == "sk-ant-secret"


@pytest.mark.asyncio
async def test_base_url_variants_share_derivation(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})
    def factory(self, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(AnthropicCompatibleProvider, "_http_client", factory)
    provider = build_provider(ProviderConfig(
        provider="anthropic_compatible",
        model="claude-test",
        base_url="https://gateway.example/v1/messages",
        api_key="k",
    ))
    try:
        await provider.generate(GenerationRequest(model="claude-test", messages=[{"role": "user", "content": "hi"}]))
    finally:
        await provider.aclose()
    assert requests[0].url.path == "/v1/messages"
