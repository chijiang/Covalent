"""sessions route group."""

from __future__ import annotations

from fastapi import APIRouter

from datetime import UTC
from datetime import datetime
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Literal
import asyncio
import json
import mimetypes

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._session_helpers import _attachment_session_dir
from covalent.api._session_helpers import _chat_upload_session_dir
from covalent.api._session_helpers import _chat_upload_visible_root
from covalent.api._session_helpers import _download_session_dir
from covalent.api._session_helpers import _next_available_upload_path
from covalent.api._session_helpers import _safe_uploaded_filename
from covalent.api._shared import _coerce_int
from covalent.api._shared import _rmtree_async
from covalent.api._shared import to_chat_session_response
from covalent.api._shared import to_chat_session_summary_response
from covalent.api.schemas import AttachmentUploadItemResponse
from covalent.api.schemas import AttachmentUploadResponse
from covalent.api.schemas import ChatSessionResponse
from covalent.api.schemas import ChatSessionSummaryResponse
from covalent.api.schemas import ChatSessionUpdateRequest
from covalent.application.services.management_service import _ensure_console_principal_can_access_session
from covalent.core.attachment_processing import process_attachment_bytes
from covalent.infra.db import DatabaseManager
from covalent.infra.memory import SessionStore
from covalent.infra.settings import AppSettings

router = APIRouter()


@router.get("/sessions")
async def list_sessions(request: Request) -> list[ChatSessionSummaryResponse]:
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    filters = {} if principal.is_admin else {"owner_user_id": principal.user_id, "workspace_id": principal.workspace_id}
    return [to_chat_session_summary_response(record) for record in await session_store.list_sessions(**filters)]

@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> ChatSessionResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    record = await session_store.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    _ensure_console_principal_can_access_session(principal, record)
    return to_chat_session_response(record)

@router.patch("/sessions/{session_id}")
async def rename_session(request: Request, session_id: str, update_request: ChatSessionUpdateRequest) -> ChatSessionResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    existing = await session_store.get_session(session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    _ensure_console_principal_can_access_session(principal, existing)
    try:
        record = await session_store.update_title(session_id, update_request.title.strip(), "manual")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
    return to_chat_session_response(record)

@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str) -> dict[str, str]:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    existing = await session_store.get_session(session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    _ensure_console_principal_can_access_session(principal, existing)
    if not await session_store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")

    async def _remove_workspace_dir() -> None:
        if settings.session_workspace_enabled:
            await _rmtree_async(settings.session_workspace_dir(session_id), ignore_errors=True)

    # Run the four filesystem cleanups concurrently off the event loop.
    await asyncio.gather(
        _rmtree_async(_attachment_session_dir(settings.workspace_root(), session_id), ignore_errors=True),
        _rmtree_async(_chat_upload_session_dir(settings, session_id), ignore_errors=True),
        _rmtree_async(_download_session_dir(settings.workspace_root(), session_id), ignore_errors=True),
        _remove_workspace_dir(),
    )
    await request.app.state.execution_backend.stop(session_id)
    return {"status": "deleted", "id": session_id}

@router.post("/attachments/upload")
async def upload_attachments(
    request: Request,
    session_id: str = Form(...),
    delivery_mode: Literal["parse", "workspace"] = Form("parse"),
    metadata_json: str = Form("[]"),
    files: list[UploadFile] = File(...),
) -> AttachmentUploadResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    existing_session = await session_store.get_session(session_id)
    if existing_session is not None:
        _ensure_console_principal_can_access_session(principal, existing_session)
    if not files:
        raise HTTPException(status_code=400, detail="At least one attachment is required")
    try:
        metadata_items = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="metadata_json must be valid JSON") from exc
    if not isinstance(metadata_items, list):
        raise HTTPException(status_code=400, detail="metadata_json must be a JSON array")

    workspace_root = _chat_upload_visible_root(settings, session_id)
    target_dir = _chat_upload_session_dir(settings, session_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    uploaded_files: list[AttachmentUploadItemResponse] = []
    for index, file in enumerate(files, start=1):
        metadata = metadata_items[index - 1] if index - 1 < len(metadata_items) and isinstance(metadata_items[index - 1], dict) else {}
        safe_name = _safe_uploaded_filename(file.filename or "", f"attachment-{index}")
        target_path = _next_available_upload_path(target_dir, safe_name)
        content = await file.read()
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File '{safe_name}' exceeds maximum upload size ({settings.max_upload_bytes} bytes)",
            )
        target_path.write_bytes(content)
        content_type = str(file.content_type or metadata.get("type") or "application/octet-stream")
        workspace_path = target_path.relative_to(workspace_root).as_posix()
        try:
            processed = process_attachment_bytes(
                file_name=target_path.name,
                content_type=content_type,
                raw_bytes=content,
                workspace_path=workspace_path,
                delivery_mode=delivery_mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        uploaded_files.append(
            AttachmentUploadItemResponse(
                name=target_path.name,
                size=len(content),
                content_type=content_type,
                last_modified=_coerce_int(metadata.get("lastModified") or metadata.get("last_modified")),
                workspace_path=workspace_path,
                uploaded_at=datetime.now(UTC),
                delivery_mode=delivery_mode,
                kind=str(processed["kind"]),
                summary=str(processed["summary"]),
                model_prompt_text=str(processed["model_prompt_text"]),
                model_content=processed["model_content"] if isinstance(processed.get("model_content"), list) else [],
                page_count=int(processed["page_count"]) if processed["page_count"] is not None else None,
            )
        )

    return AttachmentUploadResponse(session_id=session_id, files=uploaded_files)

@router.get("/downloads/{session_id}/{file_name}")
async def download_published_file(request: Request, session_id: str, file_name: str) -> FileResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, db_manager)
    existing_session = await session_store.get_session(session_id)
    if existing_session is None:
        raise HTTPException(status_code=404, detail="Unknown download")
    _ensure_console_principal_can_access_session(principal, existing_session)
    normalized_name = Path(file_name).name
    if not normalized_name or normalized_name != file_name:
        raise HTTPException(status_code=404, detail="Unknown download")

    target_path = _download_session_dir(settings.workspace_root(), session_id) / normalized_name
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="Unknown download")

    media_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
    return FileResponse(target_path, media_type=media_type, filename=target_path.name)
