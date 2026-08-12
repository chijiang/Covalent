"""API token use cases.

Extracted from ``api._auth_helpers`` into the application layer. These are the
business rules for creating / updating / revoking API tokens, independent of
the HTTP layer. Dependencies (``db_manager``, ``settings``) are passed in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from covalent.api._shared import (
    ConsolePrincipalContext,
    _api_token_summary_response,
    _coerce_positive_int,
    _dedupe_strings,
    _new_chat_item_id,
    _record_audit_log,
)
from covalent.api.auth import generate_api_token, hash_api_token
from covalent.api.schemas import (
    ApiTokenCreateRequest,
    ApiTokenCreateResponse,
    ApiTokenSummaryResponse,
    ApiTokenUpdateRequest,
)
from covalent.infra.db import ApiTokenRow, DatabaseManager, UserRow, WorkspaceRow
from covalent.infra.settings import AppSettings

def _normalize_token_scopes(scopes: list[str]) -> list[str]:
    normalized = _dedupe_strings([scope.strip() for scope in scopes if isinstance(scope, str)])
    return normalized or ["agent:invoke"]

def _normalize_token_policy(policy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(policy or {})
    if "allowed_agents" in normalized and isinstance(normalized["allowed_agents"], list):
        normalized["allowed_agents"] = _dedupe_strings([str(value) for value in normalized["allowed_agents"]])
    if "allowed_memory_modes" in normalized and isinstance(normalized["allowed_memory_modes"], list):
        allowed_modes = [str(value) for value in normalized["allowed_memory_modes"] if str(value) in {"none", "session"}]
        normalized["allowed_memory_modes"] = _dedupe_strings(allowed_modes)
    if str(normalized.get("max_trace_level") or "") not in {"none", "steps", "debug"}:
        normalized.pop("max_trace_level", None)
    for key in ("max_requests_per_minute", "max_requests_per_day", "max_tokens_per_day"):
        if key not in normalized:
            continue
        value = _coerce_positive_int(normalized[key])
        if value is None:
            normalized.pop(key, None)
        else:
            normalized[key] = value
    return normalized

async def _create_api_token(
    db_manager: DatabaseManager,
    settings: AppSettings,
    request: ApiTokenCreateRequest,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenCreateResponse:
    token, token_prefix = generate_api_token()
    token_hash = hash_api_token(token, settings.api_token_hash_pepper)
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, principal.user_id)
            workspace = await session.get(WorkspaceRow, principal.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=404, detail="Current token owner or workspace was not found")
            row = ApiTokenRow(
                id=_new_chat_item_id("token"),
                user_id=user.id,
                workspace_id=workspace.id,
                name=request.name.strip(),
                token_prefix=token_prefix,
                token_hash=token_hash,
                scopes=_normalize_token_scopes(request.scopes),
                policy_json=_normalize_token_policy(request.policy),
                expires_at=request.expires_at,
            )
            session.add(row)
            await session.flush()
            user = await session.get(UserRow, row.user_id)
            workspace = await session.get(WorkspaceRow, row.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner was not saved")
            summary = _api_token_summary_response(row, user, workspace)
    await _record_audit_log(
        db_manager,
        action="api_token.created",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id, "scopes": summary.scopes},
    )
    return ApiTokenCreateResponse(**summary.model_dump(), token=token)

async def _update_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    request: ApiTokenUpdateRequest,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenSummaryResponse:
    changed_fields = set(request.model_fields_set)
    if not changed_fields:
        raise HTTPException(status_code=400, detail="At least one API token field must be provided")

    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None or saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.revoked_at is not None:
                raise HTTPException(status_code=409, detail="Revoked API tokens cannot be updated")

            if "name" in changed_fields:
                normalized_name = (request.name or "").strip()
                if not normalized_name:
                    raise HTTPException(status_code=422, detail="API token name must not be empty")
                saved.name = normalized_name
            if "scopes" in changed_fields:
                saved.scopes = _normalize_token_scopes(request.scopes or [])
            if "policy" in changed_fields:
                saved.policy_json = _normalize_token_policy(request.policy or {})
            if "expires_at" in changed_fields:
                saved.expires_at = request.expires_at
            saved.updated_at = datetime.now(UTC)

            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner is missing")
            summary = _api_token_summary_response(saved, user, workspace)

    await _record_audit_log(
        db_manager,
        action="api_token.updated",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={
            "token_prefix": summary.token_prefix,
            "token_user_id": summary.user_id,
            "changed_fields": sorted(changed_fields),
        },
    )
    return summary

async def _revoke_api_token(
    db_manager: DatabaseManager,
    token_id: str,
    principal: ConsolePrincipalContext,
    http_request: Request | None = None,
) -> ApiTokenSummaryResponse:
    async with db_manager.session_factory() as session:
        async with session.begin():
            saved = await session.get(ApiTokenRow, token_id)
            if saved is None:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.user_id != principal.user_id or saved.workspace_id != principal.workspace_id:
                raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
            if saved.revoked_at is None:
                revoked_at = datetime.now(UTC)
                saved.revoked_at = revoked_at
                saved.updated_at = revoked_at
            user = await session.get(UserRow, saved.user_id)
            workspace = await session.get(WorkspaceRow, saved.workspace_id)
            if user is None or workspace is None:
                raise HTTPException(status_code=500, detail="API token owner is missing")
            summary = _api_token_summary_response(saved, user, workspace)
    await _record_audit_log(
        db_manager,
        action="api_token.revoked",
        target_type="api_token",
        target_id=summary.id,
        principal=principal,
        request=http_request,
        metadata={"token_prefix": summary.token_prefix, "token_user_id": summary.user_id},
    )
    return summary

