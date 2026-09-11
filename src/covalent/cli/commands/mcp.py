import asyncio

import typer

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

app = typer.Typer(help="Manage MCP servers (list/get/set/remove)", no_args_is_help=True)

_KIND = "mcp"


def _endpoint_of(item: dict) -> str:
    if item.get("transport") in {"sse", "streamable_http"}:
        return str(item.get("url") or "")
    return " ".join([str(item.get("command") or ""), *(item.get("args") or [])]).strip()


@app.command("list")
def list_mcp_servers() -> None:
    """List MCP servers."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            return await load_document(db, _KIND)

    for item in asyncio.run(run()):
        env = item.get("env") or {}
        typer.echo(
            "{name}\ttransport={transport}\tendpoint={endpoint}\tenv_vars={env_vars}\tvisibility={visibility}".format(
                name=item.get("name"),
                transport=item.get("transport", ""),
                endpoint=_endpoint_of(item),
                env_vars=len(env),
                visibility=item.get("visibility", "public"),
            )
        )


@app.command("get")
def get_mcp_server(name: str = typer.Argument(..., help="MCP server name")) -> None:
    """Print one MCP server's configuration as YAML."""
    settings = load_settings()

    async def run() -> dict | None:
        async with database_session(settings) as db:
            for item in await load_document(db, _KIND):
                if str(item.get("name") or "") == name:
                    return item
        return None

    item = asyncio.run(run())
    if item is None:
        typer.secho(f"Error: no MCP server named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.echo(yaml_dump(item), nl=False)


@app.command("set")
def set_mcp_server(
    name: str = typer.Argument(..., help="MCP server name"),
    file: str = typer.Option(..., "--file", "-f", help="YAML/JSON file with the server config ('-' = stdin)"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Create or replace an MCP server from a YAML/JSON file.

    Example (stdio):
        name: github-tools
        transport: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_TOKEN: ghp_xxx
    """
    data = load_config_file(file)
    data.pop("name", None)
    settings = load_settings()

    async def run() -> bool:
        async with database_session(settings) as db:
            return await upsert_item(db, _KIND, name, data, settings)

    created = asyncio.run(run())
    typer.echo(f"MCP server '{name}' {'created' if created else 'updated'}")
    apply_or_hint(reload)


@app.command("remove")
def remove_mcp_server(
    name: str = typer.Argument(..., help="MCP server name"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Remove an MCP server."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            await delete_item(db, _KIND, name, settings)

    asyncio.run(run())
    typer.echo(f"MCP server '{name}' removed")
    apply_or_hint(reload)
