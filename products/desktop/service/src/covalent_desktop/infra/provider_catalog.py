"""OpenAI-compatible model catalog adapter for Desktop providers."""

from __future__ import annotations

import json
from urllib.request import HTTPRedirectHandler, Request, build_opener

from covalent_desktop.application.provider_config import DesktopProviderConfig


class DesktopProviderCatalog:
    def list_models(self, provider: DesktopProviderConfig, api_key: str) -> list[str]:
        request = Request(
            f"{provider.base_url}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Provider returned an invalid model catalog")
        models = [item.get("id") for item in payload["data"] if isinstance(item, dict)]
        return sorted(
            {model for model in models if isinstance(model, str) and model.strip()}
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None
