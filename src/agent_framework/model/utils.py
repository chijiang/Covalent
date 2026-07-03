from __future__ import annotations

from typing import Any


def derive_openai_base_url(chat_url: str) -> str:
    normalized = chat_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized

def completion_token_kwargs(model: str, max_tokens: int) -> dict[str, int]:
    normalized = model.strip().lower()
    key = "max_completion_tokens" if normalized.startswith("gpt-5") else "max_tokens"
    return {key: max_tokens}


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