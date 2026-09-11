"""Manage covalent API tokens (direct database access, same trust level as other commands)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import typer
from sqlalchemy import select

from covalent.cli.runtime import database_session, load_settings

app = typer.Typer(
    help="Manage API tokens (direct DB access). The plaintext token is shown once, at create time.",
    no_args_is_help=True,
)


async def _first_admin_identity(session: Any) -> tuple[str, str]:
    """(user_id, workspace_id) of the oldest active admin; raises LookupError."""
    from covalent.infra.db import UserRow, WorkspaceMemberRow

    user = await session.scalar(
        select(UserRow)
        .where(UserRow.role == "admin", UserRow.status == "active")
        .order_by(UserRow.created_at)
        .limit(1)
    )
    if user is None:
        raise LookupError(
            "no active admin user in database — create one via `python main.py users create` + `users set-role`"
        )
    workspace_id = await session.scalar(
        select(WorkspaceMemberRow.workspace_id)
        .where(WorkspaceMemberRow.user_id == user.id)
        .limit(1)
    )
    if workspace_id is None:
        raise LookupError(f"admin user {user.email} has no workspace membership")
    return user.id, workspace_id


def _settings_with_pepper():
    settings = load_settings()
    if not settings.api_token_hash_pepper:
        raise SystemExit("AGENT_FRAMEWORK_API_TOKEN_HASH_PEPPER must be set in .env")
    return settings


def _status(row: Any, now: datetime) -> str:
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at < now:
        return "expired"
    return "active"


@app.command("list")
def list_tokens() -> None:
    """List API tokens."""
    from covalent.infra.db import ApiTokenRow

    settings = load_settings()

    async def run() -> list[Any]:
        async with database_session(settings) as db:
            async with db.session_factory() as session:
                return list(
                    (
                        await session.scalars(
                            select(ApiTokenRow).order_by(ApiTokenRow.created_at)
                        )
                    ).all()
                )

    now = datetime.now(UTC)
    for row in asyncio.run(run()):
        scopes = ",".join(row.scopes) if row.scopes else "-"
        expires = row.expires_at.isoformat() if row.expires_at else "-"
        last_used = row.last_used_at.isoformat() if row.last_used_at else "-"
        typer.echo(
            f"{row.id}\t{row.name}\t{row.token_prefix}\t{_status(row, now)}"
            f"\tscopes={scopes}\texpires={expires}\tlast_used={last_used}"
        )


@app.command("create")
def create_token(
    name: str = typer.Argument(..., help="Token name"),
    scope: list[str] = typer.Option(
        ["agent:invoke"], "--scope", help="Scopes granted to the token (repeatable)"
    ),
    expires_in_days: int | None = typer.Option(
        None,
        "--expires-in-days",
        help="Expire the token N days from now (omit = never expires)",
    ),
) -> None:
    """Create an API token owned by the first active admin user.

    The plaintext token is printed once and only its peppered hash is stored.
    """
    from covalent.application._utils import _new_chat_item_id
    from covalent.application.crypto import generate_api_token, hash_api_token
    from covalent.infra.db import ApiTokenRow

    settings = _settings_with_pepper()

    async def run() -> tuple[str, str, str]:
        async with database_session(settings) as db:
            async with db.session_factory() as session, session.begin():
                try:
                    user_id, workspace_id = await _first_admin_identity(session)
                except LookupError as exc:
                    raise SystemExit(f"Error: {exc}") from exc
                token, token_prefix = generate_api_token()
                row_id = _new_chat_item_id("token")
                session.add(
                    ApiTokenRow(
                        id=row_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        name=name,
                        token_prefix=token_prefix,
                        token_hash=hash_api_token(
                            token, settings.api_token_hash_pepper
                        ),
                        scopes=list(scope),
                        policy_json={},
                        expires_at=(
                            datetime.now(UTC) + timedelta(days=expires_in_days)
                            if expires_in_days is not None
                            else None
                        ),
                    )
                )
                return token, token_prefix, row_id

    token, token_prefix, row_id = asyncio.run(run())
    typer.echo(f"id: {row_id}")
    typer.echo(f"prefix: {token_prefix}")
    typer.secho(f"token: {token}", fg=typer.colors.GREEN)
    typer.secho(
        "store it now — it cannot be retrieved again", fg=typer.colors.YELLOW, err=True
    )


@app.command("revoke")
def revoke_token(
    token_id: str = typer.Argument(..., help="Token row id from `token list`"),
) -> None:
    """Revoke an API token; authenticated requests stop immediately."""
    from covalent.infra.db import ApiTokenRow

    settings = load_settings()

    async def run() -> str:
        async with database_session(settings) as db:
            async with db.session_factory() as session, session.begin():
                row = await session.get(ApiTokenRow, token_id)
                if row is None:
                    return f"error: no token with id '{token_id}'"
                if row.revoked_at is not None:
                    return f"error: token '{token_id}' is already revoked"
                row.revoked_at = datetime.now(UTC)
                return f"token '{token_id}' ({row.name}) revoked"

    result = asyncio.run(run())
    typer.echo(result)
    raise typer.Exit(code=2 if result.startswith("error:") else 0)
