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

app = typer.Typer(help="Manage agents (list/get/set/remove)", no_args_is_help=True)

_KIND = "agents"


def _model_of(item: dict) -> str:
    provider = item.get("provider")
    if isinstance(provider, dict):
        return str(provider.get("model") or provider.get("provider") or "")
    return ""


@app.command("list")
def list_agents() -> None:
    """List agents."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            return await load_document(db, _KIND)

    for item in asyncio.run(run()):
        typer.echo(
            "{name}\tenabled={enabled}\tvisibility={visibility}\tmodel={model}\t"
            "skills={skills}\tmcp={mcp}\tdelegates={delegates}".format(
                name=item.get("name"),
                enabled=item.get("enabled", True),
                visibility=item.get("visibility", "public"),
                model=_model_of(item),
                skills=len(item.get("skills") or []),
                mcp=len(item.get("mcp_servers") or []),
                delegates=len(item.get("delegate_agents") or []),
            )
        )


@app.command("get")
def get_agent(name: str = typer.Argument(..., help="Agent name")) -> None:
    """Print one agent's configuration as YAML (pipe to a file, edit, `set` back)."""
    settings = load_settings()

    async def run() -> dict | None:
        async with database_session(settings) as db:
            for item in await load_document(db, _KIND):
                if str(item.get("name") or "") == name:
                    return item
        return None

    item = asyncio.run(run())
    if item is None:
        typer.secho(f"Error: no agent named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.echo(yaml_dump(item), nl=False)


@app.command("set")
def set_agent(
    name: str = typer.Argument(..., help="Agent name"),
    file: str = typer.Option(..., "--file", "-f", help="YAML/JSON file with the agent config ('-' = stdin)"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Create or replace an agent from a YAML/JSON file.

    The file is the source of truth for this agent; identity metadata
    (internal_name/owner/workspace/visibility) not present in the file is
    preserved from the existing row.
    """
    data = load_config_file(file)
    data.pop("name", None)
    settings = load_settings()

    async def run() -> bool:
        async with database_session(settings) as db:
            return await upsert_item(db, _KIND, name, data, settings)

    created = asyncio.run(run())
    typer.echo(f"agent '{name}' {'created' if created else 'updated'}")
    apply_or_hint(reload)


@app.command("remove")
def remove_agent(
    name: str = typer.Argument(..., help="Agent name"),
    force: bool = typer.Option(False, "--force", help="Remove even if active delegate runs exist"),
    reload: bool = typer.Option(False, "--reload", help="Hot-reload the running service after saving"),
) -> None:
    """Remove an agent (refuses while active delegate runs reference it)."""
    settings = load_settings()

    async def run() -> None:
        async with database_session(settings) as db:
            await delete_item(db, _KIND, name, settings, force=force, check_delegates=True)

    asyncio.run(run())
    typer.echo(f"agent '{name}' removed")
    apply_or_hint(reload)
