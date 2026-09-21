"""OpenAI Responses API adapter (POST {base_url}/responses).

Maps the runtime's Chat-Completions-shaped ``GenerationRequest``/``Message``
structures to the Responses wire format and back. Two shape contracts matter
downstream and must never leak Responses shapes:

- ``ToolCall.raw`` / ``assistant_message.tool_calls`` entries are transcribed
  to the completions shape ``{"id", "type": "function", "function": {...}}``
  because ``Message.tool_calls`` is replayed verbatim into later requests and
  persisted transcript (react.py's tool-name rewriting expects it).
- ``stream_generation`` yields only ("delta"|"reasoning"|"response") tuples
  and finishes with exactly one ("response", ...) item.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from covalent.core.types import Capability, GenerationRequest, GenerationResponse, Message, TokenUsage, ToolCall
from covalent.model.base import ModelProviderError
from covalent.model.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

_VALID_EFFORTS = {"minimal", "low", "medium", "high"}
_MAX_EFFORT_FALLBACK = "high"


class ResponsesProvider(OpenAICompatibleProvider):
    _api_path = "/responses"

    @property
    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT, Capability.STREAMING, Capability.TOOL_CALLING}

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._build_request(request)
        try:
            response = await self._client.responses.create(**payload)
        except Exception as exc:
            raise self._translate_error(exc) from exc
        data = self._model_dump(response)
        return self._response_from_output(data.get("output") or [], raw_response=data)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        raise NotImplementedError("ResponsesProvider does not support raw line streaming")

    async def stream_generation(
        self, request: GenerationRequest
    ) -> AsyncIterator[tuple[str, "GenerationResponse | str"]]:
        payload = self._build_request(request, stream=True)
        final_data: dict[str, Any] | None = None
        streamed_reasoning: list[str] = []
        try:
            stream = await self._client.responses.create(**payload)
            async for event in stream:
                data = self._model_dump(event)
                event_type = data.get("type")
                if event_type == "response.output_text.delta":
                    delta = data.get("delta")
                    if isinstance(delta, str) and delta:
                        yield ("delta", delta)
                elif event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    delta = data.get("delta")
                    if isinstance(delta, str) and delta:
                        streamed_reasoning.append(delta)
                        yield ("reasoning", delta)
                elif event_type in ("response.completed", "response.incomplete"):
                    final_data = self._model_dump(data.get("response"))
                elif event_type in ("response.failed", "response.error"):
                    error = data.get("response") or data
                    detail = json.dumps(error.get("error") or error, ensure_ascii=False)
                    raise ModelProviderError(self.config.provider, detail=detail)
        except ModelProviderError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc

        if final_data is None:
            raise ModelProviderError(
                self.config.provider,
                "Responses stream ended without a completed response event",
            )
        response = self._response_from_output(final_data.get("output") or [], raw_response=final_data)
        if not response.assistant_message.reasoning_content and streamed_reasoning:
            # Gateways that omit the reasoning item from the final output: the
            # streamed summary deltas are the only record of the reasoning.
            response.assistant_message.reasoning_content = "".join(streamed_reasoning)
        yield ("response", response)

    # ------------------------------------------------------------------
    # Request mapping
    # ------------------------------------------------------------------

    def _build_request(self, request: GenerationRequest, *, stream: bool = False) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            input_items.extend(self._message_to_input_items(message))
        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "temperature": request.temperature,
        }
        if request.system_prompt:
            payload["instructions"] = request.system_prompt
        effort = self._reasoning_effort(request.reasoning_level)
        if effort:
            payload["reasoning"] = {"effort": effort}
        if request.max_tokens is not None:
            payload["max_output_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = [self._flatten_tool(tool) for tool in request.tools]
        if stream:
            payload["stream"] = True
        return payload

    @classmethod
    def _message_to_input_items(cls, message: Message) -> list[dict[str, Any]]:
        role = message.role
        if role == "tool":
            call_id = message.tool_call_id
            if not call_id:
                logger.warning("Tool result without tool_call_id dropped from Responses input")
                return []
            content = message.content
            output = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            return [{"type": "function_call_output", "call_id": call_id, "output": output}]

        if role == "assistant":
            items: list[dict[str, Any]] = []
            text = cls._extract_text(message.content)
            if text.strip():
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for raw_call in message.tool_calls or []:
                function = raw_call.get("function") if isinstance(raw_call, dict) else None
                if not isinstance(function, dict) or not function.get("name"):
                    continue
                items.append({
                    "type": "function_call",
                    "call_id": str(raw_call.get("id") or ""),
                    "name": str(function.get("name")),
                    "arguments": str(function.get("arguments") or ""),
                })
            return items

        if role == "system":
            return [{
                "role": "system",
                "content": [{"type": "input_text", "text": cls._extract_text(message.content)}],
            }]

        # user (and anything else): map content parts to Responses input parts.
        content = message.content
        if isinstance(content, str):
            return [{"role": "user", "content": [{"type": "input_text", "text": content}]}]
        parts: list[dict[str, Any]] = []
        for part in content if isinstance(content, list) else [content]:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                parts.append({"type": "input_text", "text": str(part.get("text", ""))})
            elif part_type == "image_url":
                image_url = part.get("image_url")
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if isinstance(url, str) and url:
                    parts.append({"type": "input_image", "image_url": url})
            else:
                logger.warning("Unsupported content part type '%s' dropped from Responses input", part_type)
        if not parts:
            return []
        return [{"role": "user", "content": parts}]

    @staticmethod
    def _reasoning_effort(level: str) -> str | None:
        normalized = (level or "").strip().lower()
        if not normalized or normalized in {"none", "false"}:
            return None
        if normalized in _VALID_EFFORTS:
            return normalized
        if normalized in {"xhigh", "max"}:
            return _MAX_EFFORT_FALLBACK
        logger.warning("Unknown reasoning level '%s' clamped to '%s' for Responses API", level, _MAX_EFFORT_FALLBACK)
        return _MAX_EFFORT_FALLBACK

    @staticmethod
    def _flatten_tool(schema: dict[str, Any]) -> dict[str, Any]:
        if isinstance(schema.get("function"), dict):
            function = schema["function"]
            return {
                "type": "function",
                "name": str(function.get("name")),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        # Already flat.
        return {
            "type": "function",
            "name": str(schema.get("name")),
            "description": schema.get("description"),
            "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
        }

    # ------------------------------------------------------------------
    # Response mapping
    # ------------------------------------------------------------------

    def _response_from_output(
        self,
        output_items: list[Any],
        *,
        raw_response: dict[str, Any],
    ) -> GenerationResponse:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text_parts.append(str(part.get("text", "")))
            elif item_type == "reasoning":
                for entry in item.get("summary") or []:
                    if isinstance(entry, dict) and entry.get("text"):
                        reasoning_parts.append(str(entry["text"]))
            elif item_type == "function_call":
                name = str(item.get("name") or "")
                if not name:
                    continue
                call_id = str(item.get("call_id") or "")
                arguments = item.get("arguments")
                raw_tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False),
                    },
                })

        text = "".join(text_parts)
        tool_calls = [
            ToolCall(
                id=self._raw_tool_call_id(raw_call),
                name=self._raw_tool_call_name(raw_call),
                arguments=self._parse_arguments(
                    self._raw_tool_call_arguments(raw_call),
                    provider=self.config.provider,
                    tool_name=self._raw_tool_call_name(raw_call),
                ),
                raw=raw_call,
            )
            for raw_call in raw_tool_calls
            if self._raw_tool_call_name(raw_call)
        ]
        return GenerationResponse(
            output_text=text,
            tool_calls=tool_calls,
            assistant_message=Message(
                role="assistant",
                content=text,
                tool_calls=raw_tool_calls,
                reasoning_content="".join(reasoning_parts),
            ),
            raw_response=raw_response,
            usage=self._usage_from_data(raw_response.get("usage") or {}),
        )

    @classmethod
    def _usage_from_data(cls, usage_data: dict[str, Any]) -> TokenUsage | None:
        if not isinstance(usage_data, dict):
            return None
        total = usage_data.get("total_tokens")
        input_tokens = usage_data.get("input_tokens")
        output_tokens = usage_data.get("output_tokens")
        if not total and not input_tokens and not output_tokens:
            return None

        def _int(value: Any) -> int | None:
            return int(value) if isinstance(value, (int, float)) else None

        input_details = usage_data.get("input_tokens_details")
        output_details = usage_data.get("output_tokens_details")
        return TokenUsage(
            prompt_tokens=int(input_tokens or 0),
            completion_tokens=int(output_tokens or 0),
            total_tokens=int(total or ((input_tokens or 0) + (output_tokens or 0))),
            cached_tokens=_int(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None,
            reasoning_tokens=_int(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None,
        )
