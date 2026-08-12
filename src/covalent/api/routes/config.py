"""config route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import File
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from pydantic import ValidationError
import json

from covalent.api._auth_helpers import _request_metadata
from covalent.api._auth_helpers import _resolve_console_principal
from covalent.application.schemas import ConfigDocumentResponse
from covalent.application.schemas import ConfigDocumentUpdateRequest
from covalent.application.schemas import ManagementExportResponse
from covalent.application.schemas import ManagementImportResponse
from covalent.application.schemas import PublicationRequestResponse
from covalent.application.schemas import PublicationReviewRequest
from covalent.application.services.management_service import _build_management_export_payload
from covalent.application.services.management_service import _config_document_response
from covalent.application.services.management_service import _extract_agent_renames
from covalent.application.services.management_service import _import_management_payload
from covalent.application.services.management_service import _normalize_config_kind
from covalent.application.services.management_service import _normalize_management_export_format
from covalent.application.services.management_service import _normalize_management_kind
from covalent.application.services.management_service import _request_resource_publication
from covalent.application.services.management_service import _review_resource_publication
from covalent.application.services.management_service import _serialize_management_export_payload
from covalent.application.services.management_service import _validate_config_payload
from covalent.application.services.runtime_apply import _apply_runtime_config
from covalent.infra.config_store import ConfigStore
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

router = APIRouter()


@router.get("/config/{kind}")
async def get_config(request: Request, kind: str) -> ConfigDocumentResponse:
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized = _normalize_config_kind(kind)
    payload = await config_store.get_document(normalized, principal.config)
    return _config_document_response(normalized, payload, settings)

@router.put("/config/{kind}")
async def put_config(request: Request, kind: str, update_request: ConfigDocumentUpdateRequest) -> ConfigDocumentResponse:
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized = _normalize_config_kind(kind)
    try:
        raw_payload = json.loads(update_request.raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(raw_payload, list):
        raise HTTPException(status_code=400, detail="Config payload must be a JSON array")

    try:
        validated = _validate_config_payload(normalized, raw_payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc
    agent_renames = _extract_agent_renames(update_request.metadata) if normalized == "agents" else None
    payload = await config_store.save_document(normalized, validated, principal=principal.config, agent_renames=agent_renames)
    global_payload = await config_store.get_document(normalized)
    await _apply_runtime_config(request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, normalized, global_payload)
    return _config_document_response(normalized, payload, settings)

@router.post("/config/{kind}/{resource_name}/publish-request")
async def request_config_publication(request: Request, kind: str, resource_name: str) -> PublicationRequestResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized = _normalize_config_kind(kind)
    return await _request_resource_publication(db_manager, principal, normalized, resource_name, _request_metadata(request))

@router.post("/config/{kind}/{resource_name}/publication-review")
async def review_config_publication(
    request: Request,
    kind: str,
    resource_name: str,
    review: PublicationReviewRequest,
) -> PublicationRequestResponse:
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized = _normalize_config_kind(kind)
    response = await _review_resource_publication(db_manager, principal, normalized, resource_name, review.status, request)
    global_payload = await config_store.get_document(normalized)
    await _apply_runtime_config(request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, normalized, global_payload)
    return response

@router.get("/management/{kind}/export")
async def export_management_config(request: Request, kind: str, format: str = "yaml") -> ManagementExportResponse:
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized_kind = _normalize_management_kind(kind)
    normalized_format = _normalize_management_export_format(format)
    payload, item_count = await _build_management_export_payload(request.app.state.registry, request.app.state.settings, request.app.state.config_store, normalized_kind, principal)
    content = _serialize_management_export_payload(payload, normalized_format)
    extension = "yaml" if normalized_format == "yaml" else "json"
    content_type = "application/x-yaml" if normalized_format == "yaml" else "application/json"
    return ManagementExportResponse(
        kind=normalized_kind,
        format=normalized_format,
        file_name=f"agent-framework-{normalized_kind}.{extension}",
        content_type=content_type,
        content=content,
        item_count=item_count,
    )

@router.post("/management/{kind}/import")
async def import_management_config(request: Request, kind: str, file: UploadFile = File(...)) -> ManagementImportResponse:
    settings: AppSettings = request.app.state.settings
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    normalized_kind = _normalize_management_kind(kind)
    raw = await file.read()
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Uploaded file exceeds maximum size ({settings.max_upload_bytes} bytes)",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Imported file must be UTF-8 encoded text") from exc
    return await _import_management_payload(request.app.state.db_manager, request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, normalized_kind, text, file.filename, principal, _request_metadata(request))
