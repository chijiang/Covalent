import asyncio
import sys

import typer
from sqlalchemy import select

from covalent.cli.commands._crud import (
    apply_or_hint,
    delete_item,
    load_document,
    upsert_item,
)
from covalent.cli.runtime import (
    database_session,
    load_config_file,
    load_settings,
    yaml_dump,
)

app = typer.Typer(help="Manage model providers (list/get/set/remove)", no_args_is_help=True)

_KIND = "providers"


def _mask(key: str | None) -> str:
    if not key:
        return "-"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


@app.command("list")
def list_providers() -> None:
    """List providers (API keys masked)."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            return await load_document(db, _KIND)

    for row in asyncio.run(run()):
        typer.echo(
            f"{row.get('name')}\t{row.get('provider_type')}\t{row.get('base_url')}\t{row.get('default_model')}\t"
            f"default={row.get('is_default')}\tkey={_mask(row.get('api_key'))}"
        )


@app.command("get")
def get_provider(
    name: str = typer.Argument(..., help="Provider name"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="Include the raw api_key in the output"),
) -> None:
    """Print one provider's configuration as YAML.

    The api_key is omitted unless --show-secrets; a key omitted in `set` input
    is preserved, so get → edit → set never loses credentials.
    """
    settings = load_settings()

    async def run() -> dict | None:
        async with database_session(settings) as db:
            for item in await load_document(db, _KIND):
                if str(item.get("name") or "") == name:
                    return item
        return None

    item = asyncio.run(run())
    if item is None:
        typer.secho(f"Error: no provider named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if not show_secrets:
        item = dict(item)
        if item.get("api_key"):
            item["api_key"] = None
        apih = item.get("apih")
        if isinstance(apih, dict) and apih.get("password"):
            item["apih"] = {**apih, "password": None}
    typer.echo(yaml_dump(item), nl=False)


@app.command("set")
def set_provider(
    name: str = typer.Argument(..., help="Provider name"),
    file: str = typer.Option(..., "--file", "-f", help="YAML/JSON file with the provider config ('-' = stdin)"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Create or replace a provider from a YAML/JSON file.

    Omit api_key (and apih.password) to keep the stored credential.
    Example:
        provider_type: openai_compatible
        base_url: https://api.example.com/v1
        default_model: gpt-5.4
        api_key: sk-xxx
    """
    data = load_config_file(file)
    data.pop("name", None)
    settings = load_settings()

    async def run() -> bool:
        async with database_session(settings) as db:
            return await upsert_item(db, _KIND, name, data, settings)

    created = asyncio.run(run())
    typer.echo(f"provider '{name}' {'created' if created else 'updated'}")
    apply_or_hint(reload)


@app.command("remove")
def remove_provider(
    name: str = typer.Argument(..., help="Provider name"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Remove a provider."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            await delete_item(db, _KIND, name, settings)

    asyncio.run(run())
    typer.echo(f"provider '{name}' removed")
    apply_or_hint(reload)


@app.command("set-key")
def set_key(
    name: str = typer.Argument(..., help="Provider name"),
    api_key: str | None = typer.Option(None, help="API key. Omit to be prompted (hidden input)"),
    stdin: bool = typer.Option(False, "--stdin", help="Read API key from stdin (avoids shell history)"),
) -> None:
    """Set a provider's API key directly (bypasses the config document)."""
    from covalent.infra.db import DatabaseManager, ProviderRow

    if stdin:
        api_key = sys.stdin.readline().strip()
    elif api_key is None:
        api_key = typer.prompt("API key", hide_input=True)
    if not api_key:
        raise typer.BadParameter("API key is empty")
    settings = load_settings()

    async def run() -> str:
        db = DatabaseManager(settings.database_url, schema=settings.database_schema)
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
