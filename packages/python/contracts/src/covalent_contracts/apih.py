"""APIH connection options. Secrets live in the providers store, never traces."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_endpoint(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Expected an HTTP(S) endpoint without credentials, query or fragment")
    return value.rstrip("/")


class APIHConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    token_url: str
    username: str = Field(min_length=1)
    password: str | None = Field(default=None, repr=False)
    # Matches Agent's existing environment values, which are already encoded.
    password_is_urlencoded: bool = True
    verify_tls: bool = True
    token_timeout_seconds: float = Field(default=500, gt=0, le=600)
    token_max_retries: int = Field(default=3, ge=0, le=4)

    _valid_token_url = field_validator("token_url")(validate_endpoint)

    def require_credentials(self, api_key: str | None) -> None:
        if not self.password or not api_key:
            raise ValueError("APIH requires a password and X-API-KEY")


def apih_base_url(value: str) -> str:
    return validate_endpoint(value).removesuffix("/chat/completions")
