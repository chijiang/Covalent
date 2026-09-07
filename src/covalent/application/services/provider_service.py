"""Provider catalog lookup using the same authentication as agent generation."""

from covalent.application.errors import InvalidInputError
from covalent.infra.config_store import PersistedProviderConfig
from covalent.model.base import ProviderConfig
from covalent.model.factory import build_provider


async def fetch_models(target: dict, timeout_seconds: float) -> list[str]:
    config = PersistedProviderConfig.model_validate(target)
    if not config.base_url or not config.api_key:
        raise InvalidInputError("Provider missing base_url or api_key")
    try:
        provider = build_provider(ProviderConfig(
            provider=config.provider_type, model="", base_url=config.base_url,
            api_key=config.api_key, apih=config.apih, timeout_seconds=timeout_seconds,
        ))
    except ValueError:
        raise InvalidInputError("Invalid provider connection settings or missing credentials") from None
    try:
        return await provider.list_models()
    finally:
        await provider.aclose()
