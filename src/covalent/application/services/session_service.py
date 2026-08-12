"""Chat session / transcript / title use cases.

Extracted from ``api._session_helpers`` into the application layer. Covers
transcript message construction, assistant-message updates, downloadable-attachment
metadata, session previews/titles, and pending-input resolution. Filesystem path
helpers stay in ``_session_helpers``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any


from covalent.application._utils import _coerce_int, _new_chat_item_id
from covalent.application.errors import ConflictError
from covalent.core.agent import AgentSpec
from covalent.core.types import GenerationRequest, Message, ResumedToolResult, UserInputRequest
from covalent.infra.memory import ChatActivityItem, ChatTranscriptMessage
from covalent.registry.registry import FrameworkRegistry

SSE_EVENT_INPUT_REQUIRED = "input_required"
SSE_EVENT_INPUT_RESOLVED = "input_resolved"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentRunInput:
    """Framework-independent agent run input (input content + metadata)."""

    input: str | list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

def _request_display_input(input: AgentRunInput) -> str:
    metadata = input.metadata or {}
    raw_display = metadata.get("display_input")
    if isinstance(raw_display, str):
        normalized = raw_display.strip()
        if normalized:
            return normalized
    if isinstance(input.input, str):
        return input.input
    text_parts: list[str] = []
    image_count = 0
    for item in input.input:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                text_parts.append(text)
        elif item.get("type") == "image_url":
            image_count += 1
    if text_parts:
        return "\n\n".join(text_parts)
    if image_count:
        suffix = "s" if image_count != 1 else ""
        return f"Shared {image_count} image attachment{suffix}."
    return "Message sent."

def _build_user_transcript_message(input: AgentRunInput) -> ChatTranscriptMessage:
    metadata = input.metadata or {}
    content = _request_display_input(input).strip() or "Message sent."
    attachments = metadata.get("attachments") if isinstance(metadata.get("attachments"), list) else []
    normalized_attachments = [item for item in attachments if isinstance(item, dict)]
    message_id = str(metadata.get("user_message_id") or _new_chat_item_id("user"))
    return ChatTranscriptMessage(id=message_id, role="user", content=content, attachments=normalized_attachments)

def _payload_output_text(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("output_text")
        return "" if value is None else str(value)
    return ""

def _upsert_assistant_transcript(messages: list[ChatTranscriptMessage], message_id: str, text: str) -> None:
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].content += text
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content=text))

def _replace_assistant_transcript(messages: list[ChatTranscriptMessage], message_id: str, text: str) -> None:
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].content = text
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content=text))

def _append_assistant_attachments(
    messages: list[ChatTranscriptMessage],
    message_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    if not attachments:
        return
    if messages and messages[-1].id == message_id and messages[-1].role == "assistant":
        messages[-1].attachments = _merge_attachment_metadata(messages[-1].attachments, attachments)
        return
    messages.append(ChatTranscriptMessage(id=message_id, role="assistant", content="", attachments=attachments))

def _merge_attachment_metadata(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        if not isinstance(item, dict):
            continue
        key = _attachment_metadata_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged

def _attachment_metadata_key(item: dict[str, Any]) -> str:
    for key_field in ("id", "download_url", "workspace_path", "name"):
        value = item.get(key_field)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(item, sort_keys=True, ensure_ascii=False)

def _published_download_attachments_from_tool_results(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    attachments: list[dict[str, Any]] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        if raw_result.get("name") != "publish_downloadable_file" or bool(raw_result.get("is_error")):
            continue
        content = _tool_content_json_object(raw_result.get("content"))
        if content is None:
            continue
        download_url = content.get("download_url")
        name = content.get("name")
        if not isinstance(download_url, str) or not download_url.strip() or not isinstance(name, str) or not name.strip():
            continue
        content_type = str(content.get("content_type") or "application/octet-stream")
        attachments.append(
            {
                "id": content.get("id") or f"download-{name}",
                "name": name,
                "size": _coerce_int(content.get("size")),
                "type": content_type,
                "content_type": content_type,
                "last_modified": 0,
                "workspace_path": content.get("workspace_path"),
                "download_url": download_url,
                "uploaded_at": content.get("published_at"),
                "summary": content.get("summary") or "Generated by the agent and ready to download.",
                "kind": _attachment_kind_for_content_type(content_type),
            }
        )
    return attachments

def _tool_content_json_object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

def _attachment_kind_for_content_type(content_type: str) -> str:
    normalized = content_type.strip().lower()
    if normalized == "application/pdf":
        return "pdf"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("text/"):
        return "text"
    return "binary"

def _build_session_preview(messages: list[ChatTranscriptMessage]) -> str:
    for message in reversed(messages):
        if message.content.strip():
            preview = " ".join(message.content.strip().split())
            return preview[:200]
    return ""

def _fallback_session_title(messages: list[ChatTranscriptMessage]) -> str:
    seed = next((message.content for message in messages if message.role == "user" and message.content.strip()), "")
    normalized = " ".join(seed.replace("\n", " ").split()).strip(" -:,.\t")
    if not normalized:
        return "New conversation"
    if len(normalized) <= 48:
        return normalized
    clipped = normalized[:48].rstrip(" ,.:;-")
    return f"{clipped}..."

def _normalize_generated_title(value: str) -> str:
    cleaned = value.strip().strip('"').strip("'")
    cleaned = cleaned.replace("\n", " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rstrip(" ,.:;-")
    return cleaned

async def _generate_session_title(
    registry: FrameworkRegistry,
    agent: AgentSpec,
    messages: list[ChatTranscriptMessage],
) -> str:
    fallback = _fallback_session_title(messages)
    first_user_message = next((message.content for message in messages if message.role == "user" and message.content.strip()), "")
    if not first_user_message:
        return fallback
    try:
        adapter = registry.get_model_provider(agent.provider)
        response = await adapter.generate(
            GenerationRequest(
                model=agent.provider.model,
                system_prompt=(
                    "Generate a concise conversation title. "
                    "Return plain text only, no quotes, no punctuation wrapper, 3 to 8 words."
                ),
                messages=[Message(role="user", content=first_user_message)],
                temperature=0.0,
                max_tokens=24,
            )
        )
    except Exception:
        logger.debug("Session title generation failed; using fallback", exc_info=True)
        return fallback
    return _normalize_generated_title(response.output_text) or fallback

def _extract_pending_user_input(activity: list[ChatActivityItem]) -> UserInputRequest | None:
    resolved_ids: set[str] = set()
    for item in reversed(activity):
        if item.title == SSE_EVENT_INPUT_RESOLVED and isinstance(item.payload, dict):
            resolved_id = str(item.payload.get("id", "")).strip()
            if resolved_id:
                resolved_ids.add(resolved_id)
            continue
        if item.title != SSE_EVENT_INPUT_REQUIRED:
            continue
        try:
            request = UserInputRequest.model_validate(item.payload)
        except Exception:
            logger.warning("Skipping malformed input_required activity payload: %s", item.payload, exc_info=True)
            continue
        if request.id not in resolved_ids:
            return request
    return None

def _build_resume_tool_result(
    input: AgentRunInput,
    pending_input: UserInputRequest | None,
) -> ResumedToolResult | None:
    metadata = input.metadata or {}
    resume_question_id = str(metadata.get("resume_question_id") or "").strip()
    if not resume_question_id:
        return None
    if pending_input is None:
        raise ConflictError("This session does not have a pending question to answer")
    if pending_input.id != resume_question_id:
        raise ConflictError("The pending question no longer matches the submitted answer")

    raw_answers = metadata.get("question_response")
    answers = raw_answers if isinstance(raw_answers, dict) else {"response": input.input}
    normalized_answers = {str(key): value for key, value in answers.items()}
    summary = _request_display_input(input).strip() or "Message sent."
    return ResumedToolResult(
        tool_call_id=pending_input.tool_call_id,
        tool_name=pending_input.tool_name,
        request_id=pending_input.id,
        answers=normalized_answers,
        summary=summary,
    )

