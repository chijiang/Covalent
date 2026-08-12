"""Application-layer utility helpers.

Framework-independent pure functions shared by application services. The API
layer re-exports them (see ``covalent.api._shared``) so route code keeps its
existing import names.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

_SAFE_STORAGE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")

RESOURCE_METADATA_FIELDS = (
    "owner_user_id",
    "workspace_id",
    "visibility",
    "publication_status",
    "publication_requested_at",
    "publication_reviewed_at",
    "publication_reviewed_by_user_id",
)


def _new_chat_item_id(prefix: str) -> str:
    return f"{prefix}-{int(datetime.now(UTC).timestamp() * 1000)}-{uuid4().hex[:8]}"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        results.append(normalized)
    return results


def _safe_storage_component(raw: str, default: str) -> str:
    normalized = _SAFE_STORAGE_COMPONENT_RE.sub("-", raw.strip()).strip("._-")
    return normalized or default


def _payload_text(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("text")
        return "" if value is None else str(value)
    return ""
