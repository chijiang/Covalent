import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from covalent.core.types import GenerationRequest
from covalent.model.apih import APIHProvider, APIHTokenManager
from covalent.model.apih_config import APIHConfig, apih_base_url
from covalent.model.base import ModelProviderError, ProviderConfig
from covalent.model.factory import build_provider


def settings(**updates):
    return APIHConfig(token_url="https://auth.example/token", username="user+name", password="p%26ss", **updates)


def token(**updates):
    return {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 300, **updates}


@pytest.mark.parametrize("url", ["https://gateway.example/path", "https://gateway.example/path/chat/completions/"])
def test_url_does_not_append_v1(url):
    assert apih_base_url(url) == "https://gateway.example/path"


@pytest.mark.parametrize("url", ["file:///token", "https://user:password@host/token", "https://host/token?secret=x", "https://host/token#x"])
def test_invalid_urls_rejected(url):
    with pytest.raises(ValueError):
        apih_base_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [True, False])
async def test_password_encoding_and_concurrent_cache(encoded):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=token())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = APIHTokenManager(settings(password_is_urlencoded=encoded), "x-secret", client)
        result = await asyncio.gather(*(manager.get() for _ in range(10)))
    assert len(requests) == 1
    assert result == ["Bearer access-secret"] * 10
    assert requests[0].headers["X-API-KEY"] == "x-secret"
    assert parse_qs(requests[0].content.decode()) == {
        "username": ["user+name"], "password": ["p&ss" if encoded else "p%26ss"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_ok", [True, False])
async def test_expiry_refresh_and_password_fallback(refresh_ok):
    requests = []
    def handler(request):
        payload = parse_qs(request.content.decode())
        requests.append(payload)
        if "grant_type" in payload:
            return httpx.Response(200, json=token(access_token="new")) if refresh_ok else httpx.Response(400, text="secret")
        return httpx.Response(200, json=token())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = APIHTokenManager(settings(), "x-secret", client)
        await manager.get()
        manager.expires_at = 0
        await manager.get()
    assert requests[1] == {"grant_type": ["refresh_token"], "refresh_token": ["refresh-secret"]}
    assert len(requests) == (2 if refresh_ok else 3)


@pytest.mark.asyncio
async def test_concurrent_401_only_one_new_login():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=token(access_token=str(len(calls))))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = APIHTokenManager(settings(), "x-secret", client)
        rejected = await manager.get()
        results = await asyncio.gather(*(manager.get(rejected=rejected) for _ in range(8)))
    assert results == ["Bearer 2"] * 8
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retry_and_sanitized_failure(monkeypatch):
    calls = []
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr("covalent.model.apih.asyncio.sleep", sleep)
    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="password-secret access-secret", headers={"Retry-After": "100"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        manager = APIHTokenManager(settings(token_max_retries=2), "x-secret", client)
        with pytest.raises(ModelProviderError) as error:
            await manager.get()
    assert len(calls) == 3
    assert delays == [8, 8]
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"access_token": "x", "expires_in": "bad"}, {"access_token": "x", "expires_in": "nan"}, {"access_token": "\r\nsecret"}])
async def test_malformed_token_is_sanitized(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body))) as client:
        with pytest.raises(ModelProviderError, match="token response is invalid"):
            await APIHTokenManager(settings(), "x", client).get()


def make_provider(monkeypatch, handler):
    clients = []
    def factory(self, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(APIHProvider, "_http_client", factory)
    provider = build_provider(ProviderConfig(provider="apih", model="customer-model", base_url="https://gateway.example/path/chat/completions", api_key="x-secret", apih=settings()))
    return provider, clients


@pytest.mark.asyncio
async def test_sdk_generate_401_catalog_and_cleanup(monkeypatch):
    chats = []
    logins = []
    def handler(request):
        if request.url.host == "auth.example":
            logins.append(request)
            return httpx.Response(200, json=token(access_token=str(len(logins))))
        assert request.headers["X-API-KEY"] == "x-secret"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "customer-model"}]})
        assert request.url.path == "/path/chat/completions"
        chats.append(request)
        if len(chats) == 1:
            return httpx.Response(401, text="secret")
        assert request.headers["Authorization"] == "Bearer 2"
        assert json.loads(request.content)["model"] == "customer-model"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "answer"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
    provider, clients = make_provider(monkeypatch, handler)
    try:
        response = await provider.generate(GenerationRequest(model="customer-model", messages=[{"role": "user", "content": "question"}]))
        assert response.output_text == "answer"
        assert await provider.list_models() == ["customer-model"]
    finally:
        await provider.aclose()
    assert len(logins) == 2 and len(chats) == 2
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_stream_tool_calls_and_usage(monkeypatch):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "hello", "reasoning_content": "think"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {"name": "query", "arguments": '{"x":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
    ]
    def handler(request):
        if request.url.host == "auth.example":
            return httpx.Response(200, json=token())
        assert json.loads(request.content)["stream"] is True
        assert request.headers["Authorization"] == "Bearer access-secret"
        data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=data, headers={"Content-Type": "text/event-stream"})
    provider, _ = make_provider(monkeypatch, handler)
    try:
        events = [event async for event in provider.stream_generation(GenerationRequest(model="customer-model", messages=[{"role": "user", "content": "query"}]))]
        assert ("delta", "hello") in events
        final = [value for kind, value in events if kind == "response"]
        assert len(final) == 1
        assert final[0].tool_calls[0].arguments == {"x": 1}
        assert final[0].usage.total_tokens == 5
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_second_401_not_replayed_and_error_sanitized(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=token()) if request.url.host == "auth.example" else httpx.Response(401, text="password-secret")
    provider, _ = make_provider(monkeypatch, handler)
    try:
        with pytest.raises(ModelProviderError) as error:
            await provider.generate(GenerationRequest(model="customer-model", messages=[]))
        assert "password-secret" not in str(error.value)
        assert len(calls) == 4
    finally:
        await provider.aclose()
