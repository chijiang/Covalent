import argparse
import asyncio
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Covalent entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI server")
    serve_parser.add_argument("--host", default="0.0.0.0")
    default_port = int(os.getenv("AGENT_FRAMEWORK_BACKEND_PORT", "5170"))
    serve_parser.add_argument("--port", type=int, default=default_port)

    subparsers.add_parser("migrate", help="Apply database schema migrations")

    return parser.parse_args()


def _run_migrate() -> None:
    from covalent.infra.settings import AppSettings
    from covalent.infra.migrations import run_database_migrations

    settings = AppSettings()
    database_url = settings.database_url
    if not database_url:
        raise SystemExit("AGENT_FRAMEWORK_DATABASE_URL must be set to run migrations")
    asyncio.run(run_database_migrations(database_url.replace("+asyncpg", "")))
    print("Database migrations applied.")


def main() -> None:
    args = _parse_args()

    if args.command == "migrate":
        _run_migrate()
        return

    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 5170)
    uvicorn.run("covalent.api.app:create_app", factory=True, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
