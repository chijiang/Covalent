"""providers route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.infra.config_store import ConfigStore
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings
from covalent.application.services.provider_service import fetch_models
from covalent.model.base import ModelProviderError

router = APIRouter(tags=["Providers"])


@router.get("/providers/{provider_name}/models")
async def list_provider_models(request: Request, provider_name: str) -> list[str]:
    """Fetch available models with the saved provider's authentication."""
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    providers = await config_store.get_document("providers", principal.config)
    target = None
    for p in providers:
        if p.get("name") == provider_name or p.get("internal_name") == provider_name:
            target = p
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    try:
        return await fetch_models(target, settings.request_timeout_seconds)
    except ModelProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {exc}") from exc
