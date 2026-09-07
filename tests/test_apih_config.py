import json

import pytest

from covalent.application.services.management_service import (
    _build_agent_specs, _config_document_response, _merge_provider_config,
    _resolve_default_provider, _validate_config_payload,
)
from covalent.infra.config_store import ConfigStore, PersistedProviderConfig, _resolve_provider_config
from covalent.infra.db import ProviderRow
from covalent.infra.settings import AppSettings
from covalent.model.base import ProviderConfig
from tests.test_skill_config_mcp_api import _admin_cookie, _build_app, _FakeConfigStore


def payload():
    return {
        "name": "customer", "provider_type": "apih", "base_url": "https://gateway.example/path",
        "api_key": "key-secret", "default_model": "customer-model",
        "apih": {"token_url": "https://auth.example/token", "username": "user", "password": "password-secret"},
    }


def settings():
    return AppSettings(console_auth_mode="dev", workspace_root_dir="/tmp")


def test_masked_raw_and_data_round_trip_preserve_credentials():
    stored = payload()
    result = _config_document_response("providers", [stored], settings())
    assert "password-secret" not in result.model_dump_json()
    assert "key-secret" not in result.model_dump_json()
    public = json.loads(result.raw)[0]
    assert public["apih"]["has_password"] is True
    assert public["apih"]["password"] is None
    assert stored["apih"]["password"] == "password-secret"
    public["apih"]["verify_tls"] = False
    normalized = _validate_config_payload("providers", [public])[0]
    row = ProviderRow(name="customer", api_key=stored["api_key"], apih_config=stored["apih"])
    resolved = _resolve_provider_config(row, PersistedProviderConfig.model_validate(normalized))
    assert resolved.apih.password == "password-secret"
    assert resolved.api_key == "key-secret"
    assert resolved.apih.verify_tls is False


def test_replace_and_switch_off_apih():
    row = ProviderRow(name="customer", api_key="old", apih_config=payload()["apih"])
    new = PersistedProviderConfig.model_validate(payload())
    new.apih.password = "new-password"
    assert _resolve_provider_config(row, new).apih.password == "new-password"
    new.provider_type = "openai_compatible"
    assert _resolve_provider_config(row, new).apih is None


@pytest.mark.parametrize("field", ["password", "api_key", "apih"])
def test_new_apih_requires_credentials(field):
    data = payload()
    if field == "password":
        data["apih"]["password"] = None
    else:
        data[field] = None
    with pytest.raises(ValueError):
        _resolve_provider_config(None, PersistedProviderConfig.model_validate(data))


@pytest.mark.asyncio
async def test_default_and_explicit_provider_resolution():
    default = await _resolve_default_provider(settings(), None, [payload()])
    inherited = _merge_provider_config(ProviderConfig(provider="openai_compatible", model=""), default)
    assert inherited.provider == "apih"
    assert inherited.apih.password == "password-secret"
    assert "password-secret" not in inherited.model_dump_json()
    assert "password-secret" not in inherited.cache_key()
    changed = inherited.model_copy(update={"apih": inherited.apih.model_copy(update={"password": "rotated"})})
    assert changed.cache_key() != inherited.cache_key()
    explicit = _merge_provider_config(ProviderConfig(provider="openai_compatible", model="other", base_url="https://other.example"), default)
    assert explicit.apih is None and explicit.api_key is None


def test_nondefault_apih_selected_by_existing_agent_endpoint():
    agent = {"name": "test", "description": "", "system_prompt": "", "provider": {"provider": "apih", "model": "custom", "base_url": payload()["base_url"]}}
    default = ProviderConfig(provider="openai_compatible", model="other", base_url="https://other.example", api_key="other-key")
    spec = _build_agent_specs([agent], default, [], settings(), providers_payload=[payload()])[0]
    assert spec.provider.apih.password == "password-secret"
    assert spec.provider.api_key == "key-secret"
    assert spec.provider.model == "custom"


def test_private_other_owner_provider_not_selected():
    provider = {**payload(), "owner_user_id": "other", "visibility": "private"}
    agent = {"name": "test", "description": "", "system_prompt": "", "owner_user_id": "me", "provider": {"provider": "apih", "model": "custom", "base_url": payload()["base_url"]}}
    default = ProviderConfig(provider="openai_compatible", model="other", base_url="https://other.example")
    spec = _build_agent_specs([agent], default, [], settings(), providers_payload=[provider])[0]
    assert spec.provider.apih is None and spec.provider.api_key is None


@pytest.mark.asyncio
async def test_config_store_save_load_apih_column():
    rows = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def begin(self):
            return self
        async def scalars(self, statement):
            return rows.copy()
        def add(self, row):
            rows.append(row)
        async def delete(self, row):
            rows.remove(row)
    store = ConfigStore(Session)
    await store._save_providers([payload()])
    assert rows[0].apih_config["password"] == "password-secret"
    loaded = await store._get_providers()
    assert loaded[0]["apih"]["password"] == "password-secret"
    public = _config_document_response("providers", loaded, settings()).data
    await store._save_providers(public)
    assert rows[0].apih_config["password"] == "password-secret"


def test_http_config_get_masks_password():
    app, client = _build_app(config_store=_FakeConfigStore({"providers": [payload()]}))
    response = client.get("/config/providers", headers={"Cookie": _admin_cookie(app.state.settings)})
    assert response.status_code == 200
    assert "password-secret" not in response.text
    assert response.json()["data"][0]["apih"]["has_password"] is True


def test_http_validation_does_not_echo_secrets():
    app, client = _build_app()
    data = payload()
    data["apih"]["token_timeout_seconds"] = {"invalid": "password-secret"}
    response = client.put("/config/providers", json={"raw": json.dumps([data])}, headers={"Cookie": _admin_cookie(app.state.settings)})
    assert response.status_code == 400
    assert "password-secret" not in response.text


def test_http_missing_apih_credentials_is_400():
    class Store(_FakeConfigStore):
        async def save_document(self, kind, items, **kwargs):
            _resolve_provider_config(None, PersistedProviderConfig.model_validate(items[0]))
    app, client = _build_app(config_store=Store())
    data = payload()
    data["apih"]["password"] = None
    response = client.put("/config/providers", json={"raw": json.dumps([data])}, headers={"Cookie": _admin_cookie(app.state.settings)})
    assert response.status_code == 400
