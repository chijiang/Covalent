"""Authentication / console principal helpers.

Extracted from ``app.py``. Depends only on ``_shared`` and the auth/schema/db
layers — no sibling helper modules, so no import cycles.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from jwt import PyJWTError
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from covalent.api._shared import (
    ConsolePrincipalContext,
    _new_chat_item_id,
    _safe_storage_component,
)
from covalent.infra.db import (
    DatabaseManager,
    UserRow,
    WorkspaceMemberRow,
    WorkspaceRow,
)
from covalent.infra.settings import AppSettings

from covalent.application.services.user_service import _derive_unique_username

logger = logging.getLogger(__name__)

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/auth/login",
        "/auth/register",
        "/auth/logout",
    }
)

class ConsoleAuthGuardMiddleware(BaseHTTPMiddleware):
    """Gate every non-public, non-/v1 route behind a valid console identity.

    /v1/* is intentionally left to the per-route API-token logic. This is a
    default-deny gate: any future route that forgets to resolve a principal
    is still protected unless explicitly whitelisted.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Normalize trailing slash so "/auth/login/" is still recognized.
        normalized_path = path.rstrip("/") or "/"
        settings: AppSettings | None = getattr(request.app.state, "settings", None)

        if settings is not None and (settings.console_auth_mode or "local").strip().lower() == "dev":
            return await call_next(request)
        if normalized_path in PUBLIC_PATHS or path in PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith("/v1/"):
            return await call_next(request)

        if settings is None:
            return JSONResponse(status_code=503, content={"detail": "Service is not ready"})

        try:
            _resolve_console_identity(request, settings)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)



def _console_session_secret(settings: AppSettings) -> str:
    secret = (settings.console_session_secret or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="Console session auth is not configured")
    return secret

def _console_session_max_age(settings: AppSettings) -> int:
    return max(int(settings.console_session_max_age_seconds or 0), 60)

def _console_session_cookie_secure(settings: AppSettings) -> bool:
    """Resolve the console session cookie `secure` flag.

    Explicit setting wins. Otherwise default to False only in dev auth mode
    (which is meant for local, non-TLS development); production auth modes
    default to True so the cookie never travels over plain HTTP.
    """
    if settings.console_session_cookie_secure is not None:
        return bool(settings.console_session_cookie_secure)
    return (settings.console_auth_mode or "local").strip().lower() != "dev"

def _make_console_session_token(settings: AppSettings, principal: ConsolePrincipalContext) -> str:
    now = datetime.now(UTC)
    max_age = _console_session_max_age(settings)
    return jwt.encode(
        {
            "sub": principal.user_id,
            "email": principal.email,
            "name": principal.display_name,
            "role": principal.role,
            "workspace_id": principal.workspace_id,
            "workspace_name": principal.workspace_name,
            "workspace_slug": principal.workspace_slug,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=max_age)).timestamp()),
            "jti": secrets.token_urlsafe(12),
        },
        _console_session_secret(settings),
        algorithm="HS256",
    )

def _set_console_session_cookie(response: Response, settings: AppSettings, principal: ConsolePrincipalContext) -> None:
    response.set_cookie(
        key=settings.console_session_cookie_name,
        value=_make_console_session_token(settings, principal),
        max_age=_console_session_max_age(settings),
        httponly=True,
        samesite="lax",
        secure=_console_session_cookie_secure(settings),
        path="/",
    )

def _clear_console_session_cookie(response: Response, settings: AppSettings) -> None:
    response.delete_cookie(
        key=settings.console_session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=_console_session_cookie_secure(settings),
        path="/",
    )

def _resolve_console_identity(request: Request, settings: AppSettings) -> dict[str, str]:
    mode = (settings.console_auth_mode or "local").strip().lower()
    if mode in {"local", "session", "password"}:
        return _console_identity_from_session_cookie(request, settings)
    if mode == "dev":
        return _console_identity_from_headers(request, require_identity=False)
    if mode in {"trusted_header", "trusted-headers", "headers"}:
        identity = _console_identity_from_headers(request, require_identity=True)
        _verify_trusted_header_signature(request, settings, identity)
        return identity
    if mode == "jwt":
        return _console_identity_from_jwt(request, settings)
    raise HTTPException(status_code=500, detail=f"Unsupported console auth mode: {settings.console_auth_mode}")

def _verify_trusted_header_signature(
    request: Request, settings: AppSettings, identity: dict[str, str]
) -> None:
    """When ``console_trusted_header_secret`` is configured, require the
    reverse proxy to attach an HMAC-SHA256 signature over the identity headers
    in ``x-covalent-signature``. This prevents a client that bypasses the proxy
    from forging admin identity via raw headers.

    When the secret is unset the mode trusts headers verbatim (back-compat with
    existing deployments that strip identity headers at the proxy edge);
    ``validate_runtime_secrets`` logs a loud warning in that case.
    """
    secret = (settings.console_trusted_header_secret or "").strip()
    if not secret:
        return
    provided = (request.headers.get("x-covalent-signature") or "").strip()
    if not provided:
        raise HTTPException(status_code=401, detail="Missing identity signature")
    signed = "\n".join(
        [
            identity["user_id"],
            identity["email"],
            identity["display_name"],
            identity["role"],
            identity["workspace_id"],
            identity["workspace_name"],
            identity["workspace_slug"],
        ]
    )
    expected = hmac.new(
        secret.encode("utf-8"),
        signed.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid identity signature")

def _console_identity_from_headers(request: Request, *, require_identity: bool) -> dict[str, str]:
    user_id = (request.headers.get("x-covalent-user-id") or "").strip()
    email = (request.headers.get("x-covalent-user-email") or "").strip().lower()
    if require_identity and not user_id and not email:
        raise HTTPException(status_code=401, detail="Missing console identity headers")
    return {
        "user_id": user_id,
        "email": email,
        "display_name": (request.headers.get("x-covalent-user-name") or "").strip(),
        "role": (request.headers.get("x-covalent-user-role") or "").strip().lower(),
        "workspace_id": (request.headers.get("x-covalent-workspace-id") or "").strip(),
        "workspace_name": (request.headers.get("x-covalent-workspace-name") or "").strip(),
        "workspace_slug": _safe_storage_component(request.headers.get("x-covalent-workspace-slug") or "default", "default"),
    }

def _console_identity_from_session_cookie(request: Request, settings: AppSettings) -> dict[str, str]:
    token = (request.cookies.get(settings.console_session_cookie_name) or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing console session")
    try:
        payload = jwt.decode(
            token,
            _console_session_secret(settings),
            algorithms=["HS256"],
            options={"require": ["sub", "email"]},
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid console session") from exc

    subject = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not subject or not email:
        raise HTTPException(status_code=401, detail="Console session is missing subject or email")
    return {
        "user_id": subject,
        "email": email,
        "display_name": str(payload.get("name") or payload.get("display_name") or email).strip(),
        "role": str(payload.get("role") or "member").strip().lower(),
        "workspace_id": str(payload.get("workspace_id") or "").strip(),
        "workspace_name": str(payload.get("workspace_name") or "Default workspace").strip(),
        "workspace_slug": _safe_storage_component(str(payload.get("workspace_slug") or "default"), "default"),
    }

def _console_identity_from_jwt(request: Request, settings: AppSettings) -> dict[str, str]:
    if not settings.console_auth_jwt_secret:
        raise HTTPException(status_code=500, detail="Console JWT auth is not configured")
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing console bearer token")

    options = {
        "require": ["sub", "email"],
        "verify_aud": bool(settings.console_auth_jwt_audience),
    }
    try:
        payload = jwt.decode(
            token.strip(),
            settings.console_auth_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.console_auth_jwt_issuer or None,
            audience=settings.console_auth_jwt_audience or None,
            options=options,
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid console bearer token") from exc

    subject = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not subject or not email:
        raise HTTPException(status_code=401, detail="Console bearer token is missing subject or email")
    workspace_slug = _safe_storage_component(str(payload.get("workspace_slug") or "default"), "default")
    return {
        "user_id": str(payload.get("user_id") or subject).strip(),
        "email": email,
        "display_name": str(payload.get("name") or payload.get("display_name") or email).strip(),
        "role": str(payload.get("role") or "member").strip().lower(),
        "workspace_id": str(payload.get("workspace_id") or "").strip(),
        "workspace_name": str(payload.get("workspace_name") or "Default workspace").strip(),
        "workspace_slug": workspace_slug,
    }

def _console_settings_from_request(request: Request) -> AppSettings:
    try:
        settings = request.app.state.settings
    except Exception:
        return AppSettings()
    return settings if isinstance(settings, AppSettings) else AppSettings()

async def _resolve_console_principal(request: Request, db_manager: DatabaseManager) -> ConsolePrincipalContext:
    settings = _console_settings_from_request(request)
    identity = _resolve_console_identity(request, settings)
    header_user_id = identity["user_id"]
    header_email = identity["email"]
    header_display_name = identity["display_name"]
    header_role = identity["role"]
    header_workspace_id = identity["workspace_id"]
    header_workspace_name = identity["workspace_name"]
    header_workspace_slug = identity["workspace_slug"]

    async with db_manager.session_factory() as session:
        async with session.begin():
            is_dev_identity = not header_user_id and not header_email
            if is_dev_identity:
                # Only reachable when the middleware lets an anonymous request through
                # (console_auth_mode == "dev"). Resolve to the shared local admin account
                # so dev mode keeps working without surfacing real user identity.
                fallback_email = "admin@local"
                fallback_display_name = header_display_name or "Local Admin"
            else:
                fallback_email = header_email
                fallback_display_name = header_display_name or header_email

            user: UserRow | None = await session.get(UserRow, header_user_id) if header_user_id else None
            if user is None:
                user = await session.scalar(select(UserRow).where(UserRow.email == fallback_email))

            if user is None:
                user = UserRow(
                    id=header_user_id or _new_chat_item_id("user"),
                    email=fallback_email,
                    username=await _derive_unique_username(session, fallback_email),
                    display_name=fallback_display_name,
                    role="admin" if is_dev_identity or header_role == "admin" else "member",
                    status="active",
                )
                session.add(user)
            else:
                if header_display_name:
                    user.display_name = header_display_name
                if header_role == "admin" and not user.role:
                    user.role = "admin"
                if user.status != "active":
                    raise HTTPException(status_code=403, detail="Current user is not active")

            workspace: WorkspaceRow | None = await session.get(WorkspaceRow, header_workspace_id) if header_workspace_id else None
            if workspace is None:
                workspace = await session.scalar(select(WorkspaceRow).where(WorkspaceRow.slug == header_workspace_slug))
            if workspace is None:
                workspace = WorkspaceRow(
                    id=header_workspace_id or _new_chat_item_id("workspace"),
                    name=header_workspace_name or "Default workspace",
                    slug=header_workspace_slug,
                )
                session.add(workspace)

            await session.flush()
            member = await session.get(WorkspaceMemberRow, (workspace.id, user.id))
            if member is None:
                member = WorkspaceMemberRow(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    role="admin" if user.role == "admin" else "member",
                )
                session.add(member)

            return ConsolePrincipalContext(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                preferences=dict(user.preferences_json or {}),
                role=user.role,
                workspace_id=workspace.id,
                workspace_name=workspace.name,
                workspace_slug=workspace.slug,
                workspace_role=member.role,
                username=user.username,
            )




















