"""providers route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import HTTPException
from fastapi import Request

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.infra.config_store import ConfigStore
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

router = APIRouter(tags=["Providers"])


@router.get("/providers/{provider_name}/models")
async def list_provider_models(request: Request, provider_name: str) -> list[str]:
    """Fetch available models from an OpenAI-compatible provider."""
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
    base_url = str(target.get("base_url", ""))
    api_key = target.get("api_key")
    if not base_url or not api_key:
        raise HTTPException(status_code=400, detail="Provider missing base_url or api_key")
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.request_timeout_seconds,
        )
        try:
            result = await client.models.list()
            models = sorted([m.id for m in result.data if m.id])
            return models
        finally:
            # AsyncOpenAI owns an httpx AsyncClient with a connection pool. Close
            # it explicitly so connections aren't left dangling until GC (the
            # underlying transport isn't reliably cleaned up by reference loss).
            await client.close()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {exc}") from exc
