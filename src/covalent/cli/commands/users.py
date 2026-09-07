import asyncio

import typer
from sqlalchemy import func, select

from covalent.cli.runtime import load_settings

app = typer.Typer(help="Manage platform users", no_args_is_help=True)


def _run(fn):
    return asyncio.run(fn())


async def _resolve_user(session, email: str):
    from covalent.infra.db import UserRow

    return await session.scalar(select(UserRow).where(func.lower(UserRow.email) == email.lower()))


@app.command("list")
def list_users() -> None:
    """List all users."""
    from covalent.infra.db import DatabaseManager, UserRow

    settings = load_settings()

    async def run() -> None:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                rows = list(await session.scalars(select(UserRow).order_by(UserRow.email)))
        finally:
            await db.dispose()

        for row in rows:
            typer.echo(f"{row.email}\t{row.username}\t{row.role}\t{row.status}\t{row.id}")

    _run(run)


@app.command("create")
def create_user(
    email: str = typer.Option(..., help="Email address (unique)"),
    username: str = typer.Option(..., help="Username"),
    role: str = typer.Option("member", help="Role: admin|member"),
    password: str = typer.Option(
        None, help="Initial password. Omit to be prompted (hidden input)"
    ),
    display_name: str = typer.Option("", help="Display name"),
) -> None:
    """Create a user."""
    from covalent.application._utils import _new_chat_item_id
    from covalent.application.crypto import hash_password
    from covalent.infra.db import DatabaseManager, UserRow

    if role not in {"admin", "member"}:
        raise typer.BadParameter("--role must be 'admin' or 'member'")
    settings = load_settings()
    if not password:
        password = typer.prompt("Initial password", hide_input=True, confirmation_prompt=True)

    async def run() -> str:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                async with session.begin():
                    existing = await _resolve_user(session, email)
                    if existing is not None:
                        return f"error: user with email '{email}' already exists ({existing.id})"
                    session.add(
                        UserRow(
                            id=_new_chat_item_id("user"),
                            email=email.strip().lower(),
                            username=username.strip(),
                            display_name=display_name.strip() or username.strip(),
                            password_hash=hash_password(password),
                            role=role,
                            status="active",
                        )
                    )
        finally:
            await db.dispose()
        return f"Created user '{email.strip().lower()}' ({role})"

    result = _run(run)
    typer.echo(result)
    raise typer.Exit(code=2 if result.startswith("error:") else 0)


@app.command("set-role")
def set_role(
    email: str = typer.Argument(..., help="User email"),
    role: str = typer.Argument(..., help="Role: admin|member"),
) -> None:
    """Change a user's role."""
    from covalent.infra.db import DatabaseManager

    if role not in {"admin", "member"}:
        raise typer.BadParameter("role must be 'admin' or 'member'")
    settings = load_settings()

    async def run() -> str:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                async with session.begin():
                    user = await _resolve_user(session, email)
                    if user is None:
                        return f"error: no user with email '{email}'"
                    user.role = role
        finally:
            await db.dispose()
        return f"Set role of '{email}' to {role}"

    result = _run(run)
    typer.echo(result)
    raise typer.Exit(code=2 if result.startswith("error:") else 0)


@app.command("reset-password")
def reset_password(
    email: str = typer.Argument(..., help="User email"),
    password: str = typer.Option(
        None, help="New password. Omit to be prompted (hidden input)"
    ),
) -> None:
    """Reset a user's local password."""
    from covalent.application.crypto import hash_password
    from covalent.infra.db import DatabaseManager

    settings = load_settings()
    if not password:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)

    async def run() -> str:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                async with session.begin():
                    user = await _resolve_user(session, email)
                    if user is None:
                        return f"error: no user with email '{email}'"
                    user.password_hash = hash_password(password)
        finally:
            await db.dispose()
        return f"Password reset for '{email}'"

    result = _run(run)
    typer.echo(result)
    raise typer.Exit(code=2 if result.startswith("error:") else 0)
