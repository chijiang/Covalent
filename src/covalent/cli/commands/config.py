from pathlib import Path

import typer

from covalent.cli.runtime import load_settings, run_async

app = typer.Typer(help="Export/import the platform configuration bundle", no_args_is_help=True)


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)


@app.command("export")
def export(
    output: Path = typer.Option(
        "covalent-config-bundle.zip", "--output", "-o", help="Bundle zip path to write"
    ),
    no_skills: bool = typer.Option(False, "--no-skills", help="Skip bundling local skill files"),
) -> None:
    """Export the full platform configuration (and local skills) to a bundle zip.

    The bundle contains plaintext provider API keys — store it securely.
    """
    from covalent.application.services.config_bundle_service import export_bundle

    settings = load_settings()
    summary = run_async(export_bundle, settings, output, include_skills=not no_skills)
    typer.echo(f"Bundle written to {summary['output']}")
    typer.echo(
        "  workspaces={workspaces} users={users} members={workspace_members} "
        "sandbox_profiles={sandbox_profiles}".format(**summary)
    )
    typer.echo(
        "  providers={providers} mcp_servers={mcp_servers} skill_sources={skill_sources} "
        "skill_states={skill_states} agents={agents}".format(**summary)
    )
    typer.echo(f"  skill files: {summary['skill_files']}")
    _print_warnings(summary["warnings"])


@app.command("import")
def import_command(
    bundle: Path = typer.Argument(..., exists=True, readable=True, help="Bundle zip path to import"),
    on_conflict: str = typer.Option(
        "overwrite", "--on-conflict", help="What to do when a row already exists: overwrite|skip"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and report what would change without writing"
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Abort when referenced resources (e.g. sandbox profiles) are missing"
    ),
    no_skills: bool = typer.Option(False, "--no-skills", help="Skip installing bundled skill files"),
) -> None:
    """Import a configuration bundle into this platform's database."""
    from covalent.application.services.config_bundle_service import import_bundle

    if on_conflict not in {"overwrite", "skip"}:
        raise typer.BadParameter("--on-conflict must be 'overwrite' or 'skip'")
    settings = load_settings()
    report = run_async(
        import_bundle,
        settings,
        bundle,
        on_conflict=on_conflict,
        dry_run=dry_run,
        strict=strict,
        include_skills=not no_skills,
    )
    prefix = "Dry run — would " if report.dry_run else ""
    for kind, counts in report.kinds.items():
        typer.echo(
            f"{prefix}{kind}: {counts.inserted} to insert, {counts.updated} to update"
            + (f", {counts.skipped} skipped" if counts.skipped else "")
        )
    if not report.dry_run:
        typer.echo(
            f"skills: {report.skills_installed} installed, {report.skills_replaced} replaced, "
            f"{report.skills_skipped} skipped"
        )
    _print_warnings(report.warnings)
    if report.dry_run:
        typer.echo("Dry run complete — no changes were written.")
    else:
        typer.echo("Import complete.")
