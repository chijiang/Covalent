"""Agent-compatible APIH token authentication over the existing chat adapter."""

import asyncio
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import httpx
from openai import AsyncOpenAI

from covalent.model.apih_config import APIHConfig, apih_base_url
from covalent.model.base import ModelProviderError
from covalent.model.openai_compatible import OpenAICompatibleProvider


class APIHTokenManager:
    def __init__(self, config: APIHConfig, api_key: str, client: httpx.AsyncClient):
        self.config = config
        self.api_key = api_key
        self.client = client
        self.lock = asyncio.Lock()
        self.authorization = ""
        self.expires_at = 0.0
        self.refresh_token = ""
        self.refresh_expires_at = 0.0

    async def get(self, rejected: str | None = None) -> str:
        async with self.lock:
            # Concurrent 401s must not invalidate a newer token acquired by a peer.
            if self.authorization and time.monotonic() < self.expires_at and self.authorization != rejected:
                return self.authorization
            if rejected is None and self.refresh_token and time.monotonic() < self.refresh_expires_at:
                try:
                    await self._exchange({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
                    return self.authorization
                except ModelProviderError:
                    pass  # Agent semantics: failed refresh falls back to password login.
            password = self.config.password or ""
            payload = (
                f"username={quote_plus(self.config.username)}&password={password}"
                if self.config.password_is_urlencoded
                else {"username": self.config.username, "password": password}
            )
            await self._exchange(payload)
            return self.authorization

    async def _exchange(self, payload: dict[str, str] | str) -> None:
        retry_statuses = {403, 408, 409, 429, 500, 502, 503, 504}
        for attempt in range(self.config.token_max_retries + 1):
            response = None
            try:
                response = await self.client.post(
                    self.config.token_url,
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/x-www-form-urlencoded"},
                    **({"content": payload} if isinstance(payload, str) else {"data": payload}),
                )
            except httpx.TransportError:
                if attempt == self.config.token_max_retries:
                    raise ModelProviderError("apih", "APIH token endpoint connection failed", 502) from None
            if response is not None:
                if response.is_success:
                    try:
                        self._accept(response.json())
                    except (ValueError, TypeError, KeyError, AttributeError):
                        raise ModelProviderError("apih", "APIH token response is invalid", 502) from None
                    return
                if response.status_code not in retry_statuses or attempt == self.config.token_max_retries:
                    # Never propagate gateway bodies: they can echo credentials/tokens.
                    raise ModelProviderError("apih", "APIH token endpoint rejected authentication", response.status_code)
            delay = min(0.5 * 2 ** attempt, 8)
            if response is not None:
                try:
                    delay = min(max(float(response.headers.get("Retry-After", delay)), 0), 8)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(response.headers["Retry-After"])
                        delay = min(max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0), 8)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        pass
            await asyncio.sleep(delay)

    def _accept(self, body: dict) -> None:
        token = body["access_token"]
        token_type = body.get("token_type") or "Bearer"
        if not isinstance(token, str) or not token or not isinstance(token_type, str):
            raise ValueError("Invalid token")
        authorization = f"{token_type} {token}"
        if any(ord(char) < 32 or ord(char) > 126 for char in authorization):
            raise ValueError("Invalid authorization header")
        ttl = float(body.get("expires_in", 300))
        refresh_ttl = float(body.get("refresh_expires_in", 1800))
        if not math.isfinite(ttl) or ttl <= 0 or not math.isfinite(refresh_ttl):
            raise ValueError("Invalid token lifetime")
        refresh_token = body.get("refresh_token") or ""
        if not isinstance(refresh_token, str):
            raise ValueError("Invalid refresh token")
        now = time.monotonic()
        self.authorization = authorization
        self.expires_at = now + ttl - min(30, ttl / 10)
        self.refresh_token = refresh_token
        self.refresh_expires_at = now + max(0, refresh_ttl - min(30, refresh_ttl / 10))


class APIHAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, tokens: APIHTokenManager):
        self.tokens = tokens

    async def async_auth_flow(self, request):
        authorization = await self.tokens.get()
        request.headers["Authorization"] = authorization
        response = yield request
        if response.status_code == 401:
            await response.aclose()
            request.headers["Authorization"] = await self.tokens.get(rejected=authorization)
            yield request  # Exactly one authentication replay, before any stream data.


class APIHProvider(OpenAICompatibleProvider):
    def _http_client(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(**kwargs)

    def _build_client(self) -> AsyncOpenAI:
        config = self.config.apih
        if config is None:
            raise ValueError("APIH connection settings are required")
        config.require_credentials(self.config.api_key)
        base_url = apih_base_url(self.config.base_url or "")
        self._token_client = self._http_client(timeout=config.token_timeout_seconds, verify=config.verify_tls)
        self._tokens = APIHTokenManager(config, self.config.api_key or "", self._token_client)
        return AsyncOpenAI(
            api_key="apih-token",
            base_url=base_url,
            timeout=self.config.timeout_seconds,
            max_retries=2,
            default_headers={"X-API-KEY": self.config.api_key or ""},
            http_client=self._http_client(
                auth=APIHAuth(self._tokens), timeout=self.config.timeout_seconds,
                verify=config.verify_tls,
            ),
        )

    def _translate_error(self, exc: Exception) -> ModelProviderError:
        # The SDK wraps auth-flow exceptions in APIConnectionError.
        cause: BaseException | None = exc
        for _ in range(5):
            if isinstance(cause, ModelProviderError):
                return cause
            cause = getattr(cause, "__cause__", None)
        status = getattr(exc, "status_code", None)
        if exc.__class__.__name__ == "APITimeoutError":
            status = 504
        return ModelProviderError("apih", "APIH model request failed; check endpoint, credentials and gateway logs", status or 502)

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self._token_client.aclose()
