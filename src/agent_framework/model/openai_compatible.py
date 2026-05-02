from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agent_framework.core.types import Capability, GenerationRequest, GenerationResponse, Message, ToolCall
from agent_framework.model.base import ModelAdapter, ModelProviderError, ProviderConfig


class OpenAICompatibleProvider(ModelAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        if not config.base_url:
            raise ValueError("OpenAI-compatible providers require base_url")
        super().__init__(config)
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=self._build_headers(),
            timeout=self.config.timeout_seconds,
        )

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.STREAMING,
            Capability.TOOL_CALLING,
        }

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._build_payload(request)
        data = await self._request_json(payload)

        choice = data["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                id=call.get("id"),
                name=call["function"]["name"],
                arguments=self._parse_arguments(call["function"].get("arguments")),
                raw=call,
            )
            for call in choice.get("tool_calls", [])
        ]
        assistant_message = Message(
            role=choice["role"],
            content=choice.get("content") or "",
            tool_calls=choice.get("tool_calls", []),
        )
        return GenerationResponse(
            output_text=self._extract_text(choice.get("content")),
            tool_calls=tool_calls,
            assistant_message=assistant_message,
            raw_response=data,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        payload = self._build_payload(request) | {"stream": True}
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        yield line.removeprefix("data: ")
        except httpx.HTTPError as exc:
            raise self._translate_http_error(exc) from exc

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.model_dump(exclude_none=True, exclude_defaults=True) for message in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = request.tools
        if request.system_prompt:
            payload["messages"] = [{"role": "system", "content": request.system_prompt}, *payload["messages"]]
        return payload

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            return json.loads(raw_arguments)
        return {}

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return "" if content is None else str(content)

    async def _request_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post("/chat/completions", json=payload)
            self._raise_for_status(response)
            return response.json()
        except httpx.HTTPError as exc:
            raise self._translate_http_error(exc) from exc
        except ValueError as exc:
            raise ModelProviderError(self.config.provider, f"Invalid JSON response: {exc}") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        response.raise_for_status()

    def _translate_http_error(self, exc: httpx.HTTPError) -> ModelProviderError:
        if isinstance(exc, httpx.TimeoutException):
            return ModelProviderError(
                self.config.provider,
                detail=(
                    f"Request to {self._client.base_url}chat/completions timed out after "
                    f"{self.config.timeout_seconds:.0f}s"
                ),
                status_code=504,
            )
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text.strip() or f"Upstream returned HTTP {exc.response.status_code} with an empty response body"
            return ModelProviderError(
                self.config.provider,
                detail=detail,
                status_code=exc.response.status_code,
            )
        detail = str(exc).strip() or exc.__class__.__name__
        return ModelProviderError(self.config.provider, detail=detail)

    async def aclose(self) -> None:
        await self._client.aclose()
