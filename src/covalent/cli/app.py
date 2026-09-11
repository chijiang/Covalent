import sys

import typer
from dotenv import load_dotenv

from covalent.cli.commands import (
    agents,
    config,
    health,
    mcp,
    migrate,
    providers,
    serve,
    skills,
    tokens,
    users,
)

app = typer.Typer(
    name="covalent",
    help="Covalent agent platform command line interface. Runs serve when no command is given.",
)
app.add_typer(serve.app, name="serve")
app.add_typer(migrate.app, name="migrate")
app.add_typer(config.app, name="config")
app.add_typer(users.app, name="users")
app.add_typer(providers.app, name="providers")
app.add_typer(agents.app, name="agent")
app.add_typer(mcp.app, name="mcp")
app.add_typer(skills.app, name="skill")
app.add_typer(tokens.app, name="token")
app.command(name="health", help="Check runtime health of the running service (GET /healthz)")(health.check)


def main() -> None:
    load_dotenv()
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    app()


if __name__ == "__main__":
    main()
