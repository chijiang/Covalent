"""Console user / account / admin-seed use cases.

Extracted from ``api._auth_helpers`` into the application layer. Covers user
registration, password auth, account/password updates, the seeded admin, and
console-user management. Session-cookie / identity-header parsing stays in the
API layer (``_auth_helpers``) because it is request-scoped web plumbing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from covalent.application.errors import (ConflictError, ForbiddenError, InvalidInputError, NotFoundError, UnauthorizedError)
from covalent.application._utils import _new_chat_item_id, _safe_storage_component
from covalent.application.principal import Principal as ConsolePrincipalContext
from covalent.application.crypto import hash_password, verify_password
from covalent.application.schemas import (
    ConsoleAccountUpdateRequest,
    ConsoleLoginRequest,
    ConsolePasswordUpdateRequest,
    ConsoleRegisterRequest,
    ConsoleUserResponse,
    ConsoleUserSummaryResponse,
    ConsoleUserUpdateRequest,
    USERNAME_PATTERN,
)
from covalent.infra.db import DatabaseManager, UserRow, WorkspaceMemberRow, WorkspaceRow
from covalent.infra.settings import AppSettings

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
        raise ForbiddenError("Current user is not active")

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
        raise ForbiddenError("Console sign up is disabled")
    email = request.email.strip().lower()
    username = request.username.strip().lower()
    display_name = request.display_name.strip() or username
    async with db_manager.session_factory() as session:
        async with session.begin():
            existing = await session.scalar(select(UserRow).where(UserRow.email == email))
            if existing is not None:
                raise ConflictError("A user with this email already exists")
            existing_username = await session.scalar(
                select(UserRow).where(func.lower(UserRow.username) == username)
            )
            if existing_username is not None:
                raise ConflictError("A user with this username already exists")
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
                raise UnauthorizedError("Invalid username/email or password")
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
                raise NotFoundError("Current user was not found")

            if request.username is not None and request.username != (user.username or ""):
                existing_user_id = await session.scalar(
                    select(UserRow.id).where(
                        func.lower(UserRow.username) == request.username,
                        UserRow.id != user.id,
                    )
                )
                if existing_user_id is not None:
                    raise ConflictError("A user with this username already exists")
                user.username = request.username

            if request.email is not None and request.email != user.email:
                existing_user_id = await session.scalar(
                    select(UserRow.id).where(
                        UserRow.email == request.email,
                        UserRow.id != user.id,
                    )
                )
                if existing_user_id is not None:
                    raise ConflictError("A user with this email already exists")
                user.email = request.email

            if request.display_name is not None:
                display_name = request.display_name.strip()
                if not display_name:
                    raise InvalidInputError("Display name must not be empty")
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
                raise NotFoundError("Current user was not found")
            if not user.password_hash:
                raise InvalidInputError("This account does not use a local password")
            if not verify_password(request.current_password, user.password_hash):
                raise InvalidInputError("Current password is incorrect")
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
        raise ForbiddenError("Only admins can list users")
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
        raise ForbiddenError("Only admins can update users")
    async with db_manager.session_factory() as session:
        async with session.begin():
            user = await session.get(UserRow, user_id)
            if user is None:
                raise NotFoundError(f"Unknown user: {user_id}")
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
                raise NotFoundError(f"Unknown user: {user_id}")
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

