import asyncio

import typer

from covalent.cli.runtime import (
    ReloadError,
    database_session,
    load_settings,
    reload_running_service,
)

app = typer.Typer(help="Inspect and reload skills (edit files on disk, then reload)", no_args_is_help=True)


@app.command("list")
def list_skills() -> None:
    """List skills discovered on disk and their enabled state."""
    from covalent.infra.config_store import ConfigStore
    from covalent.skills.loader import SkillLoader

    settings = load_settings()

    async def run() -> list[tuple[str, str, str, str, bool]]:
        rows: list[tuple[str, str, str, str, bool]] = []
        loader = SkillLoader(settings)
        specs = loader.discover_local()
        async with database_session(settings) as db:
            store = ConfigStore(db.session_factory)
            enabled_map = await store.get_skill_state_map()
        for spec in specs:
            rows.append(
                (
                    str(getattr(spec, "name", "")),
                    str(getattr(spec, "version", "") or ""),
                    str(getattr(getattr(spec, "runtime", None), "type", "") or ""),
                    str(getattr(spec, "source_dir", "") or ""),
                    enabled_map.get(str(getattr(spec, "name", "")), True),
                )
            )
        return rows

    skills = asyncio.run(run())
    if not skills:
        typer.echo("no skills discovered on disk")
        return
    for name, version, runtime, source_dir, enabled in skills:
        typer.echo(f"{name}\tv{version}\truntime={runtime}\tenabled={enabled}\t{source_dir}")


@app.command("reload")
def reload_skills(
    timeout: float = typer.Option(30.0, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    """Reload the running service's skills (and agents/MCP) from disk + database.

    Edit skill files directly in the skills directory, then run this — no
    restart needed. Requires an admin-owned COVALENT_API_TOKEN.
    """
    try:
        summary = reload_running_service(timeout=timeout)
    except ConnectionError as exc:
        typer.secho(
            f"Service is not reachable ({exc}); disk/db changes take effect on next start.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1) from exc
    except ReloadError as exc:
        typer.secho(f"Reload failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        "Skills reloaded: manifest_skills={skills} agents=[{agents}] mcp={mcp}".format(
            skills=summary.get("manifest_skills"),
            agents=", ".join(summary.get("agents", [])),
            mcp=summary.get("mcp_servers"),
        )
    )
