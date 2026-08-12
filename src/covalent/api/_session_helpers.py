"""Chat session / transcript / title helpers.

Extracted from ``app.py``. Depends only on ``_shared`` and core/infra layers —
no sibling helper modules, so no import cycles.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text

from covalent.api._shared import (
    _SAFE_STORAGE_COMPONENT_RE,
    _coerce_int,
    _new_chat_item_id,
    _safe_storage_component,
)
from covalent.api.schemas import AgentRunRequest
from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationRequest, Message, ResumedToolResult, UserInputRequest
from covalent.infra.memory import ChatActivityItem, ChatTranscriptMessage
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry

logger = logging.getLogger(__name__)

def _attachment_session_dir(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / ".covalent" / "attachments" / _safe_storage_component(session_id, "session")

def _chat_upload_visible_root(settings: AppSettings, session_id: str) -> Path:
    if settings.session_workspace_enabled:
        return settings.session_workspace_dir(session_id)
    return settings.workspace_root()

def _chat_upload_session_dir(settings: AppSettings, session_id: str) -> Path:
    visible_root = _chat_upload_visible_root(settings, session_id)
    if settings.session_workspace_enabled:
        return visible_root / "uploads"
    return visible_root / ".covalent" / "uploads" / _safe_storage_component(session_id, "session")

def _download_session_dir(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / ".covalent" / "downloads" / _safe_storage_component(session_id, "session")

def _safe_uploaded_filename(raw_name: str, default_stem: str) -> str:
    candidate = Path(raw_name).name.strip()
    if not candidate:
        candidate = default_stem
    parsed = Path(candidate)
    safe_stem = _safe_storage_component(parsed.stem, default_stem)
    safe_suffix = _SAFE_STORAGE_COMPONENT_RE.sub("", parsed.suffix)
    if safe_suffix and not safe_suffix.startswith("."):
        safe_suffix = f".{safe_suffix}"
    return f"{safe_stem}{safe_suffix}"

def _next_available_upload_path(directory: Path, file_name: str) -> Path:
    candidate = directory / file_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        next_candidate = directory / f"{stem}-{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


















