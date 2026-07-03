from __future__ import annotations

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5": 1_047_576,
    "gpt-5.2": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "deepseek": 393216,
    "qwen3": 64000,
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
}

DEFAULT_CONTEXT_WINDOW = 128_000


def get_context_window(model: str) -> int:
    normalized = model.strip().lower()
    for prefix, window in sorted(MODEL_CONTEXT_WINDOWS.items(), key=lambda x: -len(x[0])):
        if normalized.startswith(prefix):
            return window
    return DEFAULT_CONTEXT_WINDOW
