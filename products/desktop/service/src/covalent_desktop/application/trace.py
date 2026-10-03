"""Trace activity recorded from the runtime event stream.

Event names and payload shapes mirror the Enterprise trace vocabulary so both
products render the same runtime events with the same renderer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

DELEGATE_EVENT_PREFIX = "delegate_"
RAW_PAYLOAD_KEYS = ("raw_request", "raw_response")

# Reasoning deltas are display-only: they accumulate into the session's
# live_reasoning field while a turn runs, and the transcript the runtime
# persists when the run ends supersedes them.
REASONING_DELTA_EVENTS = frozenset({"reasoning_delta", "delegate_reasoning_delta"})
REASONING_SOURCE_MARK = "\x1e"
REASONING_SOURCE_PREFIX = "#agent:"

# Kept in the trace log. Deltas and transcript-level events (assistant_delta,
# reasoning_delta, assistant, final) are dropped: the conversation panel already
# shows that content, and persisting deltas would bloat every turn.
TRACE_BASE_EVENTS = frozenset(
    {
        "iteration",
        "model_call",
        "tool_calls",
        "tool_results",
        "thought",
        "error",
        "input_required",
        "context_window",
    }
)


def is_trace_event(event_name: str) -> bool:
    return event_name in TRACE_BASE_EVENTS or event_name.startswith(
        DELEGATE_EVENT_PREFIX
    )


def reasoning_source_marker(agent_name: str) -> str:
    """Marks the reasoning text that follows as produced by ``agent_name``.

    Mirrors the Enterprise writer (``session_service.REASONING_SOURCE_MARK``) so
    the renderer can attribute subagent segments; an empty name switches back to
    the main agent. See frontend/lib/reasoning-segments.ts.
    """
    return (
        f"{REASONING_SOURCE_MARK}{REASONING_SOURCE_PREFIX}{agent_name}"
        f"{REASONING_SOURCE_MARK}"
    )


def activity_item(event_name: str, payload: object, turn: int) -> dict[str, object]:
    # The trailing epoch milliseconds let the renderer order entries and align
    # them with the turn they belong to without storing a separate timestamp.
    return {
        "id": f"{event_name}-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid4().hex[:8]}",
        "title": event_name,
        "payload": payload,
        "turn": turn,
    }


def strip_activity_payload(
    payload: object,
) -> tuple[object, dict[str, bool]]:
    """Drop raw model payloads, which are served on demand per activity item."""
    flags = {"has_raw_request": False, "has_raw_response": False}
    if not isinstance(payload, dict):
        return payload, flags
    for key in RAW_PAYLOAD_KEYS:
        flags[f"has_{key}"] = key in payload
    if not any(flags.values()):
        return payload, flags
    return (
        {key: value for key, value in payload.items() if key not in RAW_PAYLOAD_KEYS},
        flags,
    )
