import typer

from covalent.cli.commands import config, migrate, providers, serve, users

app = typer.Typer(
    name="covalent",
    help="Covalent agent platform command line interface",
    no_args_is_help=True,
)
app.add_typer(serve.app, name="serve")
app.add_typer(migrate.app, name="migrate")
app.add_typer(config.app, name="config")
app.add_typer(users.app, name="users")
app.add_typer(providers.app, name="providers")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
