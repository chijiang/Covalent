"""Model catalog adapter for Desktop providers."""

from __future__ import annotations

import json
from urllib.request import HTTPRedirectHandler, Request, build_opener

from covalent_agent_kit.models.utils import derive_anthropic_base_url
from covalent_desktop.application.provider_config import DesktopProviderConfig

_ANTHROPIC_VERSION = "2023-06-01"


class DesktopProviderCatalog:
    def list_models(self, provider: DesktopProviderConfig, api_key: str) -> list[str]:
        url, headers = self._catalog_request(provider, api_key)
        request = Request(
            url,
            headers={**headers, "Accept": "application/json"},
        )
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Provider returned an invalid model catalog")
        models = [item.get("id") for item in payload["data"] if isinstance(item, dict)]
        return sorted(
            {model for model in models if isinstance(model, str) and model.strip()}
        )

    @staticmethod
    def _catalog_request(provider: DesktopProviderConfig, api_key: str) -> tuple[str, dict[str, str]]:
        if provider.provider_type == "anthropic_compatible":
            url = f"{derive_anthropic_base_url(provider.base_url)}/models"
            headers = {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
        else:
            url = f"{provider.base_url}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
        return url, headers


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None
