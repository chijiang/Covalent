import asyncio
import sys

import typer
from sqlalchemy import select

from covalent.cli.runtime import load_settings

app = typer.Typer(help="Manage model providers", no_args_is_help=True)


def _mask(key: str | None) -> str:
    if not key:
        return "-"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


@app.command("list")
def list_providers() -> None:
    """List providers (API keys masked)."""
    from covalent.infra.db import DatabaseManager, ProviderRow

    settings = load_settings()

    async def run() -> None:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                rows = list(
                    await session.scalars(
                        select(ProviderRow).order_by(ProviderRow.position, ProviderRow.name)
                    )
                )
        finally:
            await db.dispose()
        for row in rows:
            typer.echo(
                f"{row.name}\t{row.provider_type}\t{row.base_url}\t{row.default_model}\t"
                f"default={row.is_default}\tkey={_mask(row.api_key)}"
            )

    asyncio.run(run())


@app.command("set-key")
def set_key(
    name: str = typer.Argument(..., help="Provider name"),
    api_key: str | None = typer.Option(None, help="API key. Omit to be prompted (hidden input)"),
    stdin: bool = typer.Option(False, "--stdin", help="Read API key from stdin (avoids shell history)"),
) -> None:
    """Set a provider's API key."""
    from covalent.infra.db import DatabaseManager, ProviderRow

    if stdin:
        api_key = sys.stdin.readline().strip()
    elif api_key is None:
        api_key = typer.prompt("API key", hide_input=True)
    if not api_key:
        raise typer.BadParameter("API key is empty")
    settings = load_settings()

    async def run() -> str:
        db = DatabaseManager(settings.database_url)
        try:
            async with db.session_factory() as session:
                async with session.begin():
                    row = (
                        await session.scalars(select(ProviderRow).where(ProviderRow.name == name))
                    ).first()
                    if row is None:
                        return f"error: no provider named '{name}'"
                    row.api_key = api_key
        finally:
            await db.dispose()
        return f"API key updated for provider '{name}'"

    result = asyncio.run(run())
    typer.echo(result)
    raise typer.Exit(code=2 if result.startswith("error:") else 0)
