import argparse
import asyncio
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn

from agent_framework.api.app import create_app, sync_env_seeds


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent Framework entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI server")
    serve_parser.add_argument("--host", default="0.0.0.0")
    default_port = int(os.getenv("AGENT_FRAMEWORK_BACKEND_PORT", "5170"))
    serve_parser.add_argument("--port", type=int, default=default_port)

    sync_parser = subparsers.add_parser("sync-config", help="Sync env seed config into the database")
    sync_parser.add_argument("--overwrite", action="store_true")

    return parser.parse_args()


async def _run_sync_config(overwrite: bool) -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        result = await sync_env_seeds(app, ["mcp", "skill_sources", "agents"], overwrite=overwrite)
        for item in result.results:
            print(f"{item.kind}: {item.status} ({item.items})")


def main() -> None:
    args = _parse_args()
    if args.command == "sync-config":
        asyncio.run(_run_sync_config(args.overwrite))
        return

    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 5170)
    uvicorn.run("agent_framework.api.app:create_app", factory=True, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
