from __future__ import annotations

import inspect
import json
from json import JSONDecodeError
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from agent_framework.core.types import Capability, GenerationRequest, GenerationResponse, Message, ToolCall
from agent_framework.infra.settings import AppSettings
from agent_framework.model.base import ModelAdapter, ModelProviderError, ProviderConfig
from agent_framework.model.utils import derive_openai_base_url, reasoning_level_kwargs
from agent_framework.runtime.context_window import get_context_window



class OpenAICompatibleProvider(ModelAdapter):
    def __init__(self, config: ProviderConfig) -> None:
        if not config.base_url:
            raise ValueError("OpenAI-compatible providers require base_url")
        super().__init__(config)
        self._settings = AppSettings()
        self._client = self._build_client()

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.STREAMING,
            Capability.TOOL_CALLING,
        }

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._build_payload(request)
        try:
            response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise self._translate_error(exc) from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ModelProviderError(self.config.provider, "Upstream returned no choices")

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ModelProviderError(self.config.provider, "Upstream returned a choice without a message")

        raw_tool_calls = [self._model_dump(tool_call) for tool_call in (getattr(message, "tool_calls", None) or [])]
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
        assistant_message = Message(
            role=str(getattr(message, "role", "assistant") or "assistant"),
            content=getattr(message, "content", "") or "",
            tool_calls=raw_tool_calls,
            reasoning_content=str(getattr(message, "reasoning_content", "") or ""),
        )
        usage_data = self._model_dump(getattr(response, "usage", None)) or {}
        usage = None
        if usage_data.get("total_tokens"):
            from agent_framework.core.types import TokenUsage
            usage = TokenUsage(
                prompt_tokens=int(usage_data.get("prompt_tokens", 0)),
                completion_tokens=int(usage_data.get("completion_tokens", 0)),
                total_tokens=int(usage_data.get("total_tokens", 0)),
            )
        return GenerationResponse(
            output_text=self._extract_text(getattr(message, "content", "")),
            tool_calls=tool_calls,
            assistant_message=assistant_message,
            raw_response=self._model_dump(response),
            usage=usage,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[str]:
        payload = self._build_payload(request)
        payload["stream"] = True
        try:
            stream = await self._client.chat.completions.create(**payload)
            async for chunk in stream:
                yield json.dumps(self._model_dump(chunk), ensure_ascii=False)
        except Exception as exc:
            raise self._translate_error(exc) from exc

    def _build_client(self) -> AsyncOpenAI:
        base_url = self._normalized_base_url(self.config.base_url)
        return AsyncOpenAI(
            api_key=self.config.api_key or self._settings.default_api_key,
            base_url=base_url,
            timeout=self.config.timeout_seconds,
        )

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        messages = [message.model_dump(exclude_none=True, exclude_defaults=True) for message in request.messages]
        if request.system_prompt:
            messages = [{"role": "system", "content": request.system_prompt}, *messages]

        reasoning_kwargs = reasoning_level_kwargs(request.model, request.reasoning_level)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            **reasoning_kwargs,
        }
        if request.max_tokens is not None:
            key = "max_completion_tokens" if self._uses_max_completion_tokens(request.model) else "max_tokens"
            context_window = min(request.max_tokens or 1000000, get_context_window(request.model))
            if context_window:
                payload[key] = context_window
        if request.tools:
            payload["tools"] = request.tools
        return payload

    @staticmethod
    def _uses_max_completion_tokens(model: str) -> bool:
        return model.strip().lower().startswith("gpt-5")

    @staticmethod
    def _normalized_base_url(base_url: str | None) -> str:
        if not base_url:
            raise ValueError("OpenAI-compatible providers require base_url")
        return derive_openai_base_url(base_url)

    @staticmethod
    def _as_bool(raw: Any, *, default: bool) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _model_dump(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): cls._model_dump(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._model_dump(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return cls._model_dump(model_dump(exclude_none=True))
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return cls._model_dump(to_dict())
        if hasattr(value, "__dict__"):
            return {
                key: cls._model_dump(item)
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        return str(value)

    @staticmethod
    def _parse_arguments(raw_arguments: Any, *, provider: str, tool_name: str) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
            except JSONDecodeError as exc:
                snippet = raw_arguments[max(exc.pos - 80, 0): min(exc.pos + 80, len(raw_arguments))]
                raise ModelProviderError(
                    provider,
                    detail=(
                        f"Upstream returned invalid JSON for tool '{tool_name}' arguments: {exc}. "
                        f"Around char {exc.pos}: {snippet!r}"
                    ),
                    status_code=502,
                ) from exc
            if not isinstance(parsed, dict):
                raise ModelProviderError(
                    provider,
                    detail=(
                        f"Upstream returned non-object JSON for tool '{tool_name}' arguments. "
                        f"Expected a JSON object, got {type(parsed).__name__}."
                    ),
                    status_code=502,
                )
            return parsed
        return {}

    @staticmethod
    def _raw_tool_call_id(raw_call: dict[str, Any]) -> str | None:
        value = raw_call.get("id")
        return str(value) if value is not None else None

    @staticmethod
    def _raw_tool_call_name(raw_call: dict[str, Any]) -> str:
        function = raw_call.get("function")
        if isinstance(function, dict):
            value = function.get("name")
            return "" if value is None else str(value)
        return ""

    @staticmethod
    def _raw_tool_call_arguments(raw_call: dict[str, Any]) -> Any:
        function = raw_call.get("function")
        if isinstance(function, dict):
            return function.get("arguments")
        return None

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

    def _translate_error(self, exc: Exception) -> ModelProviderError:
        status_code = getattr(exc, "status_code", None)
        class_name = exc.__class__.__name__
        if class_name == "APITimeoutError":
            return ModelProviderError(
                self.config.provider,
                detail=(
                    f"Request to {self._normalized_base_url(self.config.base_url)}/chat/completions "
                    f"timed out after {self.config.timeout_seconds:.0f}s"
                ),
                status_code=504,
            )

        detail = str(exc).strip()
        response = getattr(exc, "response", None)
        if response is not None:
            body = getattr(response, "text", None)
            if not body:
                json_method = getattr(response, "json", None)
                if callable(json_method):
                    try:
                        body = json.dumps(json_method(), ensure_ascii=False)
                    except Exception:
                        body = None
            if body:
                detail = body.strip()

        if not detail:
            detail = class_name
        return ModelProviderError(self.config.provider, detail=detail, status_code=status_code)

    async def aclose(self) -> None:
        close_method = getattr(self._client, "close", None)
        if not callable(close_method):
            return
        result = close_method()
        if inspect.isawaitable(result):
            await result
