import typer

from covalent.cli.runtime import load_settings

app = typer.Typer(help="Apply database schema migrations")


@app.callback(invoke_without_command=True)
def migrate() -> None:
    from covalent.infra.migrations import run_database_migrations

    settings = load_settings()
    run_database_migrations(settings.database_url.replace("+asyncpg", ""))
    print("Database migrations applied.")
