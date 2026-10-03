"""Anthropic-compatible provider speaking the ``/v1/messages`` wire protocol.

Shape contract (same invariants as the OpenAI adapters):

- ``Message.tool_calls`` entries are ALWAYS completions-shaped raw dicts
  ``{"id", "type": "function", "function": {"name", "arguments"}}`` — they are
  persisted and replayed verbatim, so ``tool_use``/``tool_result`` blocks are
  transcribed in both directions.
- ``stream_generation`` yields ``("delta"|"reasoning", str)`` fragments and
  finishes with exactly one ``("response", GenerationResponse)``.
- The raw ``stream`` port yields Anthropic-shaped SSE event JSON (no production
  consumer; ``stream_generation`` is what the ReAct engine consumes).

Extended-thinking policy: reasoning signatures are not persisted, and Anthropic
requires valid signatures on thinking blocks preceding ``tool_use`` turns, so
the adapter omits the ``thinking`` parameter whenever the request history
already contains an assistant message with tool calls.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator
from json import JSONDecodeError
from typing import Any

import httpx

from covalent_runtime.domain.types import Capability, GenerationRequest, GenerationResponse, Message, TokenUsage, ToolCall
from covalent_runtime.ports.model import ModelAdapter, ModelProviderError, ProviderConfig
from covalent_agent_kit.models.utils import derive_anthropic_base_url, extract_text, parse_tool_arguments

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

_THINKING_BUDGET_TOKENS = {"low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384, "max": 16384}


class AnthropicCompatibleProvider(ModelAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        if not config.base_url:
            raise ValueError("Anthropic-compatible providers require base_url")
        super().__init__(config)
        self._base_url = derive_anthropic_base_url(config.base_url)
        self._client = self._http_client()

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.STREAMING,
            Capability.TOOL_CALLING,
        }

    def _http_client(self) -> httpx.AsyncClient:
        # Test seam: monkeypatch to return an httpx.AsyncClient backed by a
        # MockTransport (same pattern as APIHProvider).
        return httpx.AsyncClient(timeout=self.config.timeout_seconds)

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._build_payload(request)
        data = await self._post_json("/messages", payload)
        return self._response_from_data(data)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        payload = self._build_payload(request)
        payload["stream"] = True
        try:
            async with self._client.stream("POST", f"{self._base_url}/messages", json=payload, headers=self._headers()) as response:
                await self._raise_for_status(response)
                async for event in _sse_events(response.aiter_lines()):
                    yield json.dumps(event, ensure_ascii=False)
        except httpx.TimeoutException as exc:
            raise self._timeout_error() from exc
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc

    async def stream_generation(
        self, request: GenerationRequest
    ) -> AsyncIterator[tuple[str, "GenerationResponse | str"]]:
        payload = self._build_payload(request)
        payload["stream"] = True
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_blocks_by_index: dict[int, dict[str, Any]] = {}
        usage_data: dict[str, Any] = {}
        message_meta: dict[str, Any] = {}
        saw_message_start = False
        stop_reason: str | None = None
        try:
            async with self._client.stream("POST", f"{self._base_url}/messages", json=payload, headers=self._headers()) as response:
                await self._raise_for_status(response)
                async for event in _sse_events(response.aiter_lines()):
                    event_type = event.get("type")
                    if event_type == "message_start":
                        saw_message_start = True
                        message = event.get("message")
                        if isinstance(message, dict):
                            usage_data.update(message.get("usage") or {})
                            message_meta = {"id": message.get("id"), "model": message.get("model")}
                    elif event_type == "content_block_start":
                        block = event.get("content_block")
                        index = _event_index(event)
                        if isinstance(block, dict):
                            if block.get("type") == "tool_use":
                                tool_blocks_by_index[index] = {
                                    "id": block.get("id"),
                                    "name": block.get("name") or "",
                                    "input_json": "",
                                }
                            elif block.get("type") == "text":
                                initial = str(block.get("text") or "")
                                if initial:
                                    text_parts.append(initial)
                                    yield ("delta", initial)
                    elif event_type == "content_block_delta":
                        delta = event.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                text_parts.append(text)
                                yield ("delta", text)
                        elif delta_type == "thinking_delta":
                            thinking = str(delta.get("thinking") or "")
                            if thinking:
                                reasoning_parts.append(thinking)
                                yield ("reasoning", thinking)
                        elif delta_type == "input_json_delta":
                            entry = tool_blocks_by_index.setdefault(
                                _event_index(event), {"id": None, "name": "", "input_json": ""}
                            )
                            entry["input_json"] += str(delta.get("partial_json") or "")
                        # signature_delta: ignored (signatures are not persisted)
                    elif event_type == "message_delta":
                        delta = event.get("delta")
                        if isinstance(delta, dict) and delta.get("stop_reason"):
                            stop_reason = str(delta["stop_reason"])
                        if isinstance(event.get("usage"), dict):
                            usage_data.update(event["usage"])
                    elif event_type == "error":
                        error = event.get("error")
                        detail = json.dumps(error, ensure_ascii=False) if error else "Upstream stream error event"
                        raise ModelProviderError(self.config.provider, detail=detail, status_code=502)
                    # message_stop / ping / content_block_stop / unknown: ignored
        except httpx.TimeoutException as exc:
            raise self._timeout_error() from exc
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc

        if not saw_message_start:
            raise ModelProviderError(self.config.provider, "Upstream returned no message events", 502)

        text = "".join(text_parts)
        raw_tool_calls = [
            {
                "id": entry.get("id"),
                "type": "function",
                "function": {
                    "name": str(entry.get("name") or ""),
                    "arguments": str(entry.get("input_json") or ""),
                },
            }
            for _, entry in sorted(tool_blocks_by_index.items())
            if str(entry.get("name") or "")
        ]
        tool_calls = [
            ToolCall(
                id=raw_call["id"],
                name=raw_call["function"]["name"],
                arguments=parse_tool_arguments(
                    raw_call["function"]["arguments"],
                    provider=self.config.provider,
                    tool_name=raw_call["function"]["name"],
                ),
                raw=raw_call,
            )
            for raw_call in raw_tool_calls
        ]
        response = GenerationResponse(
            output_text=text,
            tool_calls=tool_calls,
            assistant_message=Message(
                role="assistant",
                content=text,
                tool_calls=raw_tool_calls,
                reasoning_content="".join(reasoning_parts),
            ),
            raw_response={
                **message_meta,
                "stop_reason": stop_reason,
                "usage": usage_data,
                "aggregated_stream": {"content": text, "tool_calls": raw_tool_calls},
            },
            usage=self._usage_from_data(usage_data),
        )
        yield ("response", response)

    async def list_models(self) -> list[str]:
        data = await self._post_json("/models", None, method="GET")
        items = data.get("data") if isinstance(data, dict) else None
        models = [item.get("id") for item in (items or []) if isinstance(item, dict)]
        return sorted({model for model in models if isinstance(model, str) and model.strip()})

    async def aclose(self) -> None:
        close_method = getattr(self._client, "aclose", None)
        if not callable(close_method):
            return
        result = close_method()
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------ request

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        if request.system_prompt:
            system_parts.append(request.system_prompt)
        message_params: list[dict[str, Any]] = []
        index = 0
        messages = request.messages
        while index < len(messages):
            message = messages[index]
            if message.role == "system":
                text = extract_text(message.content).strip()
                if text:
                    system_parts.append(text)
                index += 1
            elif message.role == "tool":
                tool_results: list[dict[str, Any]] = []
                while index < len(messages) and messages[index].role == "tool":
                    tool_message = messages[index]
                    index += 1
                    if not tool_message.tool_call_id:
                        logger.warning(
                            "Dropping tool result without tool_call_id (provider=%s)", self.config.provider
                        )
                        continue
                    content = tool_message.content
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False, default=str)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_message.tool_call_id,
                            "content": content,
                        }
                    )
                if tool_results:
                    message_params.append({"role": "user", "content": tool_results})
            elif message.role == "assistant":
                blocks = self._assistant_blocks(message)
                if blocks:
                    message_params.append({"role": "assistant", "content": blocks})
                index += 1
            else:
                content = self._user_content(message)
                if content:
                    message_params.append({"role": "user", "content": content})
                index += 1

        max_tokens = self._resolve_max_tokens(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max_tokens,
            "messages": message_params,
        }
        system_text = "\n\n".join(part for part in system_parts if part.strip())
        if system_text:
            payload["system"] = system_text

        budget = self._thinking_budget(request.reasoning_level)
        history_has_tool_calls = any(
            message.role == "assistant" and message.tool_calls for message in messages
        )
        if budget is not None and not history_has_tool_calls:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # max_tokens must exceed budget_tokens; keep a small generation headroom.
            if max_tokens <= budget:
                payload["max_tokens"] = budget + 1024
        else:
            payload["temperature"] = request.temperature

        tools = [tool for tool in (self._anthropic_tool(schema) for schema in request.tools) if tool]
        if tools:
            payload["tools"] = tools
        return payload

    def _resolve_max_tokens(self, request: GenerationRequest) -> int:
        if request.max_tokens is not None and request.max_tokens > 0:
            return int(request.max_tokens)
        raw = self.config.extra.get("anthropic_max_tokens", "")
        try:
            configured = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_MAX_TOKENS
        return configured if configured > 0 else DEFAULT_MAX_TOKENS

    @classmethod
    def _thinking_budget(cls, level: str) -> int | None:
        normalized = (level or "none").strip().lower()
        if normalized in {"", "none", "false"}:
            return None
        budget = _THINKING_BUDGET_TOKENS.get(normalized)
        if budget is None:
            logger.warning("Unknown reasoning level %r for Anthropic; disabling thinking", level)
            return None
        return budget

    @staticmethod
    def _anthropic_tool(schema: dict[str, Any]) -> dict[str, Any] | None:
        """Map a completions-shaped (or already flat) tool schema to /v1/messages form."""
        function = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        if not isinstance(function, dict):
            return None
        name = str(function.get("name") or "")
        if not name:
            return None
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        return {
            "name": name,
            "description": str(function.get("description") or ""),
            "input_schema": parameters,
        }

    @classmethod
    def _user_content(cls, message: Message) -> list[dict[str, Any]]:
        content = message.content
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        parts: list[dict[str, Any]] = []
        for item in content or []:
            if not isinstance(item, dict):
                logger.warning("Dropping non-dict user content part (provider=%s)", cls.__name__)
                continue
            part_type = item.get("type")
            if part_type == "text":
                text = str(item.get("text", ""))
                if text:
                    parts.append({"type": "text", "text": text})
            elif part_type == "image_url":
                image_url = item.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if url:
                    parts.append({"type": "image", "source": {"type": "url", "url": str(url)}})
            else:
                logger.warning("Dropping unsupported user content part type %r", part_type)
        return parts

    @staticmethod
    def _assistant_blocks(message: Message) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        text = extract_text(message.content)
        if text:
            blocks.append({"type": "text", "text": text})
        for raw_call in message.tool_calls or []:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            if not name:
                continue
            blocks.append(
                {
                    "type": "tool_use",
                    "id": raw_call.get("id"),
                    "name": name,
                    "input": parse_tool_arguments(
                        function.get("arguments"),
                        provider="history",
                        tool_name=name,
                    ),
                }
            )
        return blocks

    # ----------------------------------------------------------------- response

    def _response_from_data(self, data: dict[str, Any]) -> GenerationResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block_type == "thinking":
                reasoning_parts.append(str(block.get("thinking") or ""))
            elif block_type == "tool_use":
                raw_tool_calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
            # redacted_thinking and unknown block types: ignored
        tool_calls = [
            ToolCall(
                id=raw_call["id"],
                name=raw_call["function"]["name"],
                arguments=parse_tool_arguments(
                    raw_call["function"]["arguments"],
                    provider=self.config.provider,
                    tool_name=raw_call["function"]["name"],
                ),
                raw=raw_call,
            )
            for raw_call in raw_tool_calls
            if raw_call["function"]["name"]
        ]
        text = "".join(text_parts)
        return GenerationResponse(
            output_text=text,
            tool_calls=tool_calls,
            assistant_message=Message(
                role="assistant",
                content=text,
                tool_calls=raw_tool_calls,
                reasoning_content="".join(reasoning_parts),
            ),
            raw_response=data,
            usage=self._usage_from_data(data.get("usage")),
        )

    @classmethod
    def _usage_from_data(cls, usage_data: Any) -> TokenUsage | None:
        if not isinstance(usage_data, dict):
            return None

        def _int(value: Any) -> int:
            return int(value) if isinstance(value, (int, float)) else 0

        # Anthropic's input_tokens excludes cache reads/creation; fold them in so
        # budget accounting upstream stays conservative-correct.
        cache_read = _int(usage_data.get("cache_read_input_tokens"))
        cache_creation = _int(usage_data.get("cache_creation_input_tokens"))
        prompt_tokens = _int(usage_data.get("input_tokens")) + cache_read + cache_creation
        completion_tokens = _int(usage_data.get("output_tokens"))
        if not (prompt_tokens or completion_tokens):
            return None
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            reasoning_tokens=None,
            cached_tokens=cache_read or None,
        )

    # -------------------------------------------------------------------- http

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self.config.api_key:
            headers["x-api-key"] = self.config.api_key
        return headers

    async def _post_json(self, path: str, payload: dict[str, Any] | None, *, method: str = "POST") -> dict[str, Any]:
        try:
            if method == "GET":
                response = await self._client.get(f"{self._base_url}{path}", headers=self._headers())
            else:
                response = await self._client.post(f"{self._base_url}{path}", json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise self._timeout_error() from exc
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc
        await self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ModelProviderError(
                self.config.provider, detail="Upstream returned a non-JSON response", status_code=502
            ) from exc
        if not isinstance(data, dict):
            raise ModelProviderError(
                self.config.provider, detail="Upstream returned a non-object JSON response", status_code=502
            )
        return data

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            body = (await response.aread()).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise ModelProviderError(
            self.config.provider,
            detail=self._detail_from_body(body) or f"HTTP {response.status_code}",
            status_code=response.status_code,
        )

    @staticmethod
    def _detail_from_body(body: str) -> str:
        text = (body or "").strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str) and error:
                return error
        return text

    def _timeout_error(self) -> ModelProviderError:
        return ModelProviderError(
            self.config.provider,
            detail=(
                f"Request to {self._base_url}/messages timed out after {self.config.timeout_seconds:.0f}s"
            ),
            status_code=504,
        )

    def _transport_error(self, exc: Exception) -> ModelProviderError:
        return ModelProviderError(self.config.provider, detail=exc.__class__.__name__, status_code=502)


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Parse an Anthropic SSE byte stream into JSON events.

    Collects ``data:`` payload lines, dispatches on blank lines, and ignores
    ``event:``/``id:`` fields (Anthropic duplicates the event type inside the
    JSON payload). Tolerates a ``[DONE]`` sentinel and non-JSON keepalives.
    """
    data_lines: list[str] = []

    def _emit(payload: str) -> dict[str, Any] | None:
        payload = payload.strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    async for line in lines:
        if not line:
            if data_lines:
                event = _emit("\n".join(data_lines))
                data_lines.clear()
                if event is not None:
                    yield event
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # event:/id:/retry:/comment lines: ignored
    if data_lines:
        event = _emit("\n".join(data_lines))
        if event is not None:
            yield event


def _event_index(event: dict[str, Any]) -> int:
    raw = event.get("index")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
