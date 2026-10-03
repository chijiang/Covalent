from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from covalent_runtime.ports.model import ModelProviderError


def derive_openai_base_url(chat_url: str) -> str:
    normalized = chat_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def derive_anthropic_base_url(base_url: str) -> str:
    """Normalize a base URL to an Anthropic API root ending in "/v1".

    Accepts the API root ("https://api.anthropic.com"), a versioned root
    (".../v1"), or a pasted full endpoint (".../v1/messages"). Gateway paths
    beyond "/v1" are not supported.
    """
    normalized = base_url.strip().rstrip("/")
    for suffix in ("/v1/messages", "/messages", "/v1"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return f"{normalized}/v1"


def parse_tool_arguments(raw_arguments: Any, *, provider: str, tool_name: str) -> dict[str, Any]:
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


def extract_text(content: Any) -> str:
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


def reasoning_level_kwargs(model: str, reasoning_level: str = 'none') -> dict[str, Any]:
    """Return model-specific reasoning/thinking kwargs for a given model and level.

    Supported levels: none, low, medium, high, xhigh, max.
    """
    normalized = model.strip().lower()
    if not reasoning_level:
        reasoning_level = 'none'
    level = reasoning_level.strip().lower()

    # gpt-5 family — native reasoning_effort
    if normalized.startswith("gpt-5"):
        if level in ('none', 'false'):
            return {}
        return {"reasoning_effort": level}

    # deepseek-v4 family — thinking toggle + reasoning_effort mapping
    if normalized.startswith("deepseek-v4"):
        if level in ('none', 'false'):
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        effort_map = {
            "low": "high",
            "medium": "high",
            "high": "high",
            "xhigh": "max",
            "max": "max",
        }
        effort = effort_map.get(level, "high")
        return {
            "reasoning_effort": effort,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    # qwen3 family — enable_thinking flag
    if normalized.startswith("qwen3"):
        if level in ('none', 'false'):
            return {"extra_body": {"enable_thinking": False}}
        else:
            return {"extra_body": {"enable_thinking": True}}

    return {}