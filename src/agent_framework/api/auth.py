from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_framework.infra.db import ApiTokenRow, UserRow
from agent_framework.infra.settings import AppSettings


TOKEN_PREFIX = "cvt_"
TOKEN_SECRET_BYTES = 32
PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_SALT_BYTES = 16
PASSWORD_ITERATIONS = 210_000


@dataclass(frozen=True)
class ApiPrincipal:
    user_id: str
    workspace_id: str
    token_id: str
    token_prefix: str
    scopes: frozenset[str]
    policy: dict[str, Any]


def hash_api_token(token: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_api_token() -> tuple[str, str]:
    token_id = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
    token = f"{TOKEN_PREFIX}{token_id}_{secret}"
    return token, token_id


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        scheme, iterations_raw, salt, expected = password_hash.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    if scheme != PASSWORD_SCHEME or iterations < 1 or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token.strip()


def _extract_token_prefix(token: str) -> str:
    if not token.startswith(TOKEN_PREFIX):
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    remainder = token.removeprefix(TOKEN_PREFIX)
    public_part, separator, secret = remainder.partition("_")
    if not public_part or not separator or not secret:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return public_part


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


async def authenticate_api_token(
    request: Request,
    *,
    settings: AppSettings,
    session_factory: async_sessionmaker[AsyncSession],
) -> ApiPrincipal:
    token = _extract_bearer_token(request)
    token_prefix = _extract_token_prefix(token)
    token_hash = hash_api_token(token, settings.api_token_hash_pepper)
    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            row = await session.scalar(select(ApiTokenRow).where(ApiTokenRow.token_prefix == token_prefix))
            if row is None or not hmac.compare_digest(row.token_hash, token_hash):
                raise HTTPException(status_code=401, detail="Invalid bearer token")
            if row.revoked_at is not None:
                raise HTTPException(status_code=401, detail="API token has been revoked")
            if _is_expired(row.expires_at, now):
                raise HTTPException(status_code=401, detail="API token has expired")

            user = await session.get(UserRow, row.user_id)
            if user is None or user.status != "active":
                raise HTTPException(status_code=403, detail="Token owner is not active")

            row.last_used_at = now
            return ApiPrincipal(
                user_id=row.user_id,
                workspace_id=row.workspace_id,
                token_id=row.id,
                token_prefix=row.token_prefix,
                scopes=frozenset(row.scopes or []),
                policy=dict(row.policy_json or {}),
            )


def require_scope(principal: ApiPrincipal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(status_code=403, detail=f"Missing required scope: {scope}")


def require_agent_allowed(principal: ApiPrincipal, agent_name: str) -> None:
    allowed_agents = principal.policy.get("allowed_agents")
    if allowed_agents is None:
        return
    if not isinstance(allowed_agents, list) or agent_name not in {str(item) for item in allowed_agents}:
        raise HTTPException(status_code=403, detail=f"Token is not allowed to invoke agent: {agent_name}")


def require_memory_mode_allowed(principal: ApiPrincipal, memory_mode: Literal["none", "session"]) -> None:
    allowed_modes = principal.policy.get("allowed_memory_modes")
    if allowed_modes is None:
        return
    if not isinstance(allowed_modes, list) or memory_mode not in {str(item) for item in allowed_modes}:
        raise HTTPException(status_code=403, detail=f"Token is not allowed to use memory mode: {memory_mode}")


def require_trace_level_allowed(principal: ApiPrincipal, trace_level: Literal["none", "steps", "debug"]) -> None:
    order = {"none": 0, "steps": 1, "debug": 2}
    max_level = str(principal.policy.get("max_trace_level") or "steps")
    if max_level not in order:
        max_level = "steps"
    if order[trace_level] > order[max_level]:
        raise HTTPException(status_code=403, detail=f"Token is not allowed to use trace level: {trace_level}")
