"""Authentication / console principal helpers.

Extracted from ``app.py``. Depends only on ``_shared`` and the auth/schema/db
layers — no sibling helper modules, so no import cycles.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import PyJWTError
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from agent_framework.api._shared import (
    ConsolePrincipalContext,
    _agent_run_log_response,
    _api_token_summary_response,
    _audit_log_response,
    _new_chat_item_id,
    _safe_storage_component,
    _usage_int,
)
from agent_framework.api.auth import hash_password, verify_password
from agent_framework.api.schemas import (
    AgentRunLogResponse,
    ApiTokenSummaryResponse,
    ApiTokenUsageByTokenResponse,
    ApiTokenUsageDailyResponse,
    ApiTokenUsageResponse,
    AuditLogResponse,
    ConsoleAccountUpdateRequest,
    ConsoleLoginRequest,
    ConsolePasswordUpdateRequest,
    ConsoleRegisterRequest,
    ConsoleUserResponse,
    ConsoleUserSummaryResponse,
    ConsoleUserUpdateRequest,
    USERNAME_PATTERN,
)
from agent_framework.infra.db import (
    AgentRunLogRow,
    ApiTokenRow,
    AuditLogRow,
    DatabaseManager,
    UserRow,
    WorkspaceMemberRow,
    WorkspaceRow,
)
from agent_framework.infra.settings import AppSettings

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

async def _derive_unique_username(session: AsyncSession, email: str) -> str:
    """Derive a valid, unique username from an email for auto-provisioned users.

    The ``username`` column is NOT NULL with a case-insensitive unique index, so
    every user-creation path must supply one. Registration and the seed receive
    one from input/settings; this covers the session auto-provision path, which
    only has the caller's email. The local-part is sanitized to the username
    alphabet and, on collision, suffixed with an incrementing number.
    """
    local_part = (email or "").split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9_-]", "-", local_part).strip("-_")
    if not USERNAME_PATTERN.match(base):
        base = "user"
    base = base[:27]  # leave room for a "-NNN" suffix within the 32-char limit
    candidate = base
    suffix = 1
    while (
        await session.scalar(
            select(UserRow.id).where(func.lower(UserRow.username) == candidate)
        )
        is not None
    ):
        candidate = f"{base}-{suffix}"[-32:]
        suffix += 1
    return candidate

async def _principal_for_user(
    session: AsyncSession,
    user: UserRow,
    *,
    workspace_name: str = "Default workspace",
    workspace_slug: str = "default",
) -> ConsolePrincipalContext:
    if user.status != "active":
        raise HTTPException(status_code=403, detail="Current user is not active")

    member = await session.scalar(select(WorkspaceMemberRow).where(WorkspaceMemberRow.user_id == user.id))
    workspace: WorkspaceRow | None = None
    if member is not None:
        workspace = await session.get(WorkspaceRow, member.workspace_id)
        if workspace is None:
            # Orphan membership: workspace row is gone but membership remains.
            await session.delete(member)
            await session.flush()
            member = None

    if workspace is None:
        existing_workspace_count = await session.scalar(select(func.count(WorkspaceRow.id)))
        safe_slug = _safe_storage_component(workspace_slug or "default", "default")
        if int(existing_workspace_count or 0) == 0:
            workspace = WorkspaceRow(
                id=_new_chat_item_id("workspace"),
                name=workspace_name.strip() or "Default workspace",
                slug=safe_slug,
            )
            session.add(workspace)
        else:
            workspace = await session.scalar(select(WorkspaceRow).where(WorkspaceRow.slug == safe_slug))
            if workspace is None:
                workspace = WorkspaceRow(
                    id=_new_chat_item_id("workspace"),
                    name=workspace_name.strip() or "Default workspace",
                    slug=safe_slug,
                )
                session.add(workspace)
        # Flush parents first so workspace_members FK inserts cannot race ahead of workspaces.
        await session.flush()
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

async def _register_console_user(
    db_manager: DatabaseManager,
    settings: AppSettings,
    request: ConsoleRegisterRequest,
) -> ConsolePrincipalContext:
    if not settings.console_signup_enabled:
        raise HTTPException(status_code=403, detail="Console sign up is disabled")
    email = request.email.strip().lower()
    username = request.username.strip().lower()
    display_name = request.display_name.strip() or username
    async with db_manager.session_factory() as session:
        async with session.begin():
            existing = await session.scalar(select(UserRow).where(UserRow.email == email))
            if existing is not None:
                raise HTTPException(status_code=409, detail="A user with this email already exists")
            existing_username = await session.scalar(
                select(UserRow).where(func.lower(UserRow.username) == username)
            )
            if existing_username is not None:
                raise HTTPException(status_code=409, detail="A user with this username already exists")
            existing_user_count = await session.scalar(select(func.count(UserRow.id)))
            user = UserRow(
                id=_new_chat_item_id("user"),
                email=email,
                username=username,
                display_name=display_name,
                password_hash=hash_password(request.password),
                role="admin" if int(existing_user_count or 0) == 0 else "member",
                status="active",
            )
            session.add(user)
            return await _principal_for_user(
                session,
                user,
                workspace_name=request.workspace_name,
                workspace_slug=_safe_storage_component(request.workspace_name or "default", "default"),
            )

async def _authenticate_console_password(
    db_manager: DatabaseManager,
    request: ConsoleLoginRequest,
) -> ConsolePrincipalContext:
    identifier = request.identifier.strip().lower()
    async with db_manager.session_factory() as session:
        async with session.begin():
            if "@" in identifier:
                user = await session.scalar(select(UserRow).where(UserRow.email == identifier))
            else:
                user = await session.scalar(select(UserRow).where(func.lower(UserRow.username) == identifier))
            if user is None or not verify_password(request.password, user.password_hash):
                raise HTTPException(status_code=401, detail="Invalid username/email or password")
            return await _principal_for_user(session, user)

async def _update_current_account(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    request: ConsoleAccountUpdateRequest,
) -> ConsolePrincipalContext:
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, principal.user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="Current user was not found")

            if request.username is not None and request.username != (user.username or ""):
                existing_user_id = await session.scalar(
                    select(UserRow.id).where(
                        func.lower(UserRow.username) == request.username,
                        UserRow.id != user.id,
                    )
                )
                if existing_user_id is not None:
                    raise HTTPException(status_code=409, detail="A user with this username already exists")
                user.username = request.username

            if request.email is not None and request.email != user.email:
                existing_user_id = await session.scalar(
                    select(UserRow.id).where(
                        UserRow.email == request.email,
                        UserRow.id != user.id,
                    )
                )
                if existing_user_id is not None:
                    raise HTTPException(status_code=409, detail="A user with this email already exists")
                user.email = request.email

            if request.display_name is not None:
                display_name = request.display_name.strip()
                if not display_name:
                    raise HTTPException(status_code=422, detail="Display name must not be empty")
                user.display_name = display_name

            if "avatar_url" in request.model_fields_set:
                user.avatar_url = request.avatar_url

            if request.preferences is not None:
                user.preferences_json = request.preferences.model_dump()

            await session.flush()
            return await _principal_for_user(
                session,
                user,
                workspace_name=principal.workspace_name,
                workspace_slug=principal.workspace_slug,
            )

async def _update_current_password(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    request: ConsolePasswordUpdateRequest,
) -> None:
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, principal.user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="Current user was not found")
            if not user.password_hash:
                raise HTTPException(status_code=400, detail="This account does not use a local password")
            if not verify_password(request.current_password, user.password_hash):
                raise HTTPException(status_code=400, detail="Current password is incorrect")
            user.password_hash = hash_password(request.new_password)

async def _seed_initial_admin_user(db_manager: DatabaseManager, settings: AppSettings) -> None:
    if not settings.console_seed_admin_enabled:
        return
    username = settings.console_seed_admin_username.strip().lower()
    email = settings.console_seed_admin_email.strip().lower() or f"{username}@local"
    password = settings.console_seed_admin_password
    if not username or not password:
        return
    async with db_manager.session_factory() as session:
        async with session.begin():
            by_email = await session.scalar(select(UserRow).where(UserRow.email == email))
            by_username = await session.scalar(
                select(UserRow).where(func.lower(UserRow.username) == username)
            )
            # Prefer the username match when email/username resolve to different rows so
            # we never stamp the seed username onto another account (unique violation).
            if by_email is not None and by_username is not None and by_email.id != by_username.id:
                user = by_username
            else:
                user = by_email or by_username
            if user is None:
                user = UserRow(
                    id=_new_chat_item_id("user"),
                    email=email,
                    username=username,
                    display_name=settings.console_seed_admin_display_name.strip() or username,
                    password_hash=hash_password(password),
                    role="admin",
                    status="active",
                )
                session.add(user)
            else:
                if not user.password_hash:
                    user.password_hash = hash_password(password)
                if not user.display_name.strip():
                    user.display_name = settings.console_seed_admin_display_name.strip() or username
                if not user.username:
                    username_taken = await session.scalar(
                        select(UserRow.id).where(
                            func.lower(UserRow.username) == username,
                            UserRow.id != user.id,
                        )
                    )
                    if username_taken is None:
                        user.username = username
                if not user.email or "@" not in user.email:
                    email_taken = await session.scalar(
                        select(UserRow.id).where(
                            UserRow.email == email,
                            UserRow.id != user.id,
                        )
                    )
                    if email_taken is None:
                        user.email = email
                user.role = "admin"
                user.status = "active"

            await _principal_for_user(
                session,
                user,
                workspace_name=settings.console_seed_admin_workspace_name,
                workspace_slug=_safe_storage_component(settings.console_seed_admin_workspace_name or "default", "default"),
            )

def _console_user_response(principal: ConsolePrincipalContext) -> ConsoleUserResponse:
    return ConsoleUserResponse(
        user_id=principal.user_id,
        username=principal.username,
        email=principal.email,
        display_name=principal.display_name,
        avatar_url=principal.avatar_url,
        preferences=principal.preferences,
        role=principal.role,
        workspace_id=principal.workspace_id,
        workspace_name=principal.workspace_name,
        workspace_role=principal.workspace_role,
    )

def _console_user_summary_response(
    user: UserRow,
    workspace: WorkspaceRow | None = None,
    member: WorkspaceMemberRow | None = None,
) -> ConsoleUserSummaryResponse:
    return ConsoleUserSummaryResponse(
        user_id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        workspace_id=workspace.id if workspace is not None else None,
        workspace_name=workspace.name if workspace is not None else None,
        workspace_role=member.role if member is not None else None,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )

async def _list_console_users(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
) -> list[ConsoleUserSummaryResponse]:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can list users")
    async with db_manager.session_factory() as session:
        rows = (
            await session.execute(
                select(UserRow, WorkspaceRow, WorkspaceMemberRow)
                .outerjoin(WorkspaceMemberRow, WorkspaceMemberRow.user_id == UserRow.id)
                .outerjoin(WorkspaceRow, WorkspaceRow.id == WorkspaceMemberRow.workspace_id)
                # Scope to the admin's own workspace so one tenant cannot enumerate
                # another tenant's users. The outerjoins above are retained so the
                # row shape passed to _console_user_summary_response is unchanged.
                .where(WorkspaceMemberRow.workspace_id == principal.workspace_id)
                .order_by(
                    UserRow.created_at.desc(),
                    UserRow.email.asc(),
                    # Deterministic tiebreak so a user with multiple workspace
                    # memberships always resolves to the same row below.
                    WorkspaceRow.created_at.asc(),
                )
            )
        ).all()
        # The joins above fan out to one row per workspace membership, so a user
        # belonging to more than one workspace would otherwise appear several
        # times (same user_id) and break the console's keyed list. Collapse to a
        # single entry per user, keeping the first (earliest) membership.
        summaries: list[ConsoleUserSummaryResponse] = []
        seen_user_ids: set[str] = set()
        for user, workspace, member in rows:
            if user.id in seen_user_ids:
                continue
            seen_user_ids.add(user.id)
            summaries.append(_console_user_summary_response(user, workspace, member))
        return summaries

async def _update_console_user(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    user_id: str,
    request: ConsoleUserUpdateRequest,
) -> ConsoleUserSummaryResponse:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can update users")
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
            # Scope the update to the admin's own workspace: a user who is not a
            # member of this workspace is treated as unknown, so one tenant's
            # admin cannot mutate another tenant's users.
            member = await session.scalar(
                select(WorkspaceMemberRow).where(
                    WorkspaceMemberRow.user_id == user.id,
                    WorkspaceMemberRow.workspace_id == principal.workspace_id,
                )
            )
            if member is None:
                raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
            user_changed = False
            if request.display_name is not None:
                user.display_name = request.display_name.strip()
                user_changed = True
            if request.role is not None:
                user.role = request.role
                user_changed = True
            if request.status is not None:
                user.status = request.status
                user_changed = True
            # Assign in Python so onupdate=func.now() does not expire the attr on flush (MissingGreenlet).
            if user_changed:
                user.updated_at = datetime.now(UTC)

            if request.workspace_role is not None:
                member.role = request.workspace_role
                member.updated_at = datetime.now(UTC)
            workspace = await session.get(WorkspaceRow, member.workspace_id)

            return _console_user_summary_response(user, workspace, member)

async def _list_api_token_summaries(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
) -> list[ApiTokenSummaryResponse]:
    async with db_manager.session_factory() as session:
        stmt = (
            select(ApiTokenRow, UserRow, WorkspaceRow)
            .join(UserRow, ApiTokenRow.user_id == UserRow.id)
            .join(WorkspaceRow, ApiTokenRow.workspace_id == WorkspaceRow.id)
            .order_by(ApiTokenRow.created_at.desc())
        )
        stmt = stmt.where(
            ApiTokenRow.user_id == principal.user_id,
            ApiTokenRow.workspace_id == principal.workspace_id,
        )
        rows = (
            await session.execute(stmt)
        ).all()
        return [_api_token_summary_response(token, user, workspace) for token, user, workspace in rows]




async def _list_api_token_runs(
    db_manager: DatabaseManager,
    token_id: str,
    principal: ConsolePrincipalContext,
    *,
    limit: int = 50,
) -> list[AgentRunLogResponse]:
    bounded_limit = min(max(limit, 1), 200)
    async with db_manager.session_factory() as session:
        token = await session.get(ApiTokenRow, token_id)
        if token is None:
            raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
        if token.user_id != principal.user_id or token.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail=f"Unknown API token: {token_id}")
        rows = (
            await session.execute(
                select(AgentRunLogRow)
                .where(AgentRunLogRow.token_id == token_id)
                .order_by(AgentRunLogRow.created_at.desc())
                .limit(bounded_limit)
            )
        ).scalars()
        return [_agent_run_log_response(row) for row in rows]

def _build_api_token_usage_response(
    tokens: list[ApiTokenRow],
    runs: list[AgentRunLogRow],
    *,
    days: int,
    now: datetime,
) -> ApiTokenUsageResponse:
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    daily_metrics: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        day = (starts_at + timedelta(days=offset)).date().isoformat()
        daily_metrics[day] = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "latencies": [],
        }

    token_metrics: dict[str, dict[str, Any]] = {
        token.id: {
            "token": token,
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "latencies": [],
        }
        for token in tokens
    }

    total_requests = 0
    successful_requests = 0
    failed_requests = 0
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    latencies: list[int] = []

    for run in runs:
        if run.token_id not in token_metrics or run.created_at is None:
            continue
        created_at = run.created_at if run.created_at.tzinfo is not None else run.created_at.replace(tzinfo=UTC)
        if created_at < starts_at or created_at > now:
            continue
        day_key = created_at.date().isoformat()
        day = daily_metrics.get(day_key)
        if day is None:
            continue

        usage = dict(run.usage_json or {})
        run_input_tokens = _usage_int(usage, "input_tokens", "prompt_tokens")
        run_output_tokens = _usage_int(usage, "output_tokens", "completion_tokens")
        run_total_tokens = _usage_int(usage, "total_tokens") or run_input_tokens + run_output_tokens
        succeeded = run.status == "completed"

        total_requests += 1
        successful_requests += int(succeeded)
        failed_requests += int(not succeeded)
        total_tokens += run_total_tokens
        input_tokens += run_input_tokens
        output_tokens += run_output_tokens
        if run.latency_ms is not None:
            latencies.append(run.latency_ms)

        day["requests"] += 1
        day["successful_requests"] += int(succeeded)
        day["failed_requests"] += int(not succeeded)
        day["total_tokens"] += run_total_tokens
        day["input_tokens"] += run_input_tokens
        day["output_tokens"] += run_output_tokens
        if run.latency_ms is not None:
            day["latencies"].append(run.latency_ms)

        token = token_metrics[run.token_id]
        token["requests"] += 1
        token["successful_requests"] += int(succeeded)
        token["failed_requests"] += int(not succeeded)
        token["total_tokens"] += run_total_tokens
        if run.latency_ms is not None:
            token["latencies"].append(run.latency_ms)

    active_tokens = sum(
        1
        for token in tokens
        if token.revoked_at is None
        and (
            token.expires_at is None
            or (token.expires_at if token.expires_at.tzinfo is not None else token.expires_at.replace(tzinfo=UTC)) > now
        )
    )

    daily = [
        ApiTokenUsageDailyResponse(
            date=day,
            requests=metrics["requests"],
            successful_requests=metrics["successful_requests"],
            failed_requests=metrics["failed_requests"],
            total_tokens=metrics["total_tokens"],
            input_tokens=metrics["input_tokens"],
            output_tokens=metrics["output_tokens"],
            average_latency_ms=(
                round(sum(metrics["latencies"]) / len(metrics["latencies"])) if metrics["latencies"] else None
            ),
        )
        for day, metrics in daily_metrics.items()
    ]

    by_token = [
        ApiTokenUsageByTokenResponse(
            token_id=token_id,
            token_name=metrics["token"].name,
            token_prefix=metrics["token"].token_prefix,
            requests=metrics["requests"],
            successful_requests=metrics["successful_requests"],
            failed_requests=metrics["failed_requests"],
            total_tokens=metrics["total_tokens"],
            average_latency_ms=(
                round(sum(metrics["latencies"]) / len(metrics["latencies"])) if metrics["latencies"] else None
            ),
            last_used_at=metrics["token"].last_used_at,
        )
        for token_id, metrics in sorted(
            token_metrics.items(),
            key=lambda item: (-item[1]["requests"], item[1]["token"].name.lower()),
        )
    ]

    return ApiTokenUsageResponse(
        days=days,
        starts_at=starts_at,
        ends_at=now,
        active_tokens=active_tokens,
        total_requests=total_requests,
        successful_requests=successful_requests,
        failed_requests=failed_requests,
        total_tokens=total_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        average_latency_ms=round(sum(latencies) / len(latencies)) if latencies else None,
        daily=daily,
        by_token=by_token,
    )

async def _get_api_token_usage(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    *,
    days: int = 30,
) -> ApiTokenUsageResponse:
    bounded_days = min(max(days, 1), 365)
    now = datetime.now(UTC)
    starts_at = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=bounded_days - 1)
    async with db_manager.session_factory() as session:
        tokens = list(
            (
                await session.execute(
                    select(ApiTokenRow)
                    .where(
                        ApiTokenRow.user_id == principal.user_id,
                        ApiTokenRow.workspace_id == principal.workspace_id,
                    )
                    .order_by(ApiTokenRow.created_at.desc())
                )
            ).scalars()
        )
        token_ids = [token.id for token in tokens]
        runs: list[AgentRunLogRow] = []
        if token_ids:
            runs = list(
                (
                    await session.execute(
                        select(AgentRunLogRow)
                        .where(
                            AgentRunLogRow.token_id.in_(token_ids),
                            AgentRunLogRow.created_at >= starts_at,
                            AgentRunLogRow.created_at <= now,
                        )
                        .order_by(AgentRunLogRow.created_at.asc())
                    )
                ).scalars()
            )
    return _build_api_token_usage_response(tokens, runs, days=bounded_days, now=now)

async def _list_audit_logs(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    *,
    limit: int = 100,
    action: str | None = None,
    outcome: str | None = None,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
    target_type: str | None = None,
) -> list[AuditLogResponse]:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can list audit logs")
    bounded_limit = min(max(limit, 1), 500)
    async with db_manager.session_factory() as session:
        stmt = (
            select(AuditLogRow)
            # Scope to the admin's own workspace so one tenant cannot read
            # another tenant's audit trail (which includes IPs, user agents, run metadata).
            .where(AuditLogRow.workspace_id == principal.workspace_id)
            .order_by(AuditLogRow.created_at.desc())
            .limit(bounded_limit)
        )
        if actor_user_id:
            stmt = stmt.where(AuditLogRow.actor_user_id == actor_user_id)
        if action:
            stmt = stmt.where(AuditLogRow.action == action)
        if outcome:
            stmt = stmt.where(AuditLogRow.outcome == outcome)
        if actor_token_id:
            stmt = stmt.where(AuditLogRow.actor_token_id == actor_token_id)
        if target_type:
            stmt = stmt.where(AuditLogRow.target_type == target_type)
        rows = (await session.execute(stmt)).scalars()
        return [_audit_log_response(row) for row in rows]

