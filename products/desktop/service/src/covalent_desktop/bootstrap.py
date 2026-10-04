"""Desktop service process lifecycle."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
from collections.abc import Callable
from typing import TextIO

from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import DesktopWorkspace
from covalent_desktop.infra.built_in_skills import sync_bundled_skills
from covalent_desktop.infra.local_store import LocalStore
from covalent_desktop.infra.agent_registry import DesktopRegistryFactory
from covalent_desktop.infra.provider_catalog import DesktopProviderCatalog
from covalent_desktop.infra.skill_manager import DesktopSkillManager
from covalent_desktop.infra.mcp_inspector import DesktopMcpInspector

LOGGER = logging.getLogger(__name__)
TOKEN_ENV = "COVALENT_DESKTOP_SERVICE_TOKEN"
MIN_TOKEN_LENGTH = 32


def run_service(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    ready_stream: TextIO = sys.stdout,
) -> int:
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"{TOKEN_ENV} must contain at least {MIN_TOKEN_LENGTH} characters"
        )

    data_dir = Path(
        os.environ.get(
            "COVALENT_DESKTOP_DATA_DIR", Path.home() / ".covalent" / "desktop"
        )
    )
    store = LocalStore(data_dir / "desktop.sqlite3")
    try:
        synced = sync_bundled_skills(data_dir / "skills" / "built_in")
        if synced:
            LOGGER.info("Synced built-in skills: %s", ", ".join(synced))
    except OSError as error:
        LOGGER.warning("Could not sync built-in skills: %s", error)
    registry_factory = DesktopRegistryFactory(data_dir, store)
    server = create_server(
        host,
        port,
        token,
        DesktopWorkspace(
            store,
            registry_factory,
            registry_factory.available_skills,
            DesktopProviderCatalog(),
            registry_factory.available_local_tools,
            DesktopSkillManager(registry_factory.skill_loader, data_dir / "skills"),
            DesktopMcpInspector(),
        ),
        downloads_root=registry_factory.downloads_root(),
    )
    registry_factory.service_base_url = (
        f"http://{host}:{server.server_address[1]}/downloads"
    )
    status = server.service_status
    ready = {
        "type": "ready",
        "host": host,
        "port": server.server_address[1],
        "pid": os.getpid(),
        "protocol_version": status.protocol_version,
        "service_version": status.service_version,
        "capabilities": list(status.capabilities),
    }
    ready_stream.write(json.dumps(ready, separators=(",", ":")) + "\n")
    ready_stream.flush()

    restore_handlers: list[tuple[int, signal.Handlers]] = []

    def request_shutdown(_signum: int, _frame: object) -> None:
        # BaseServer.shutdown must be called from a different thread.
        threading.Thread(
            target=server.shutdown, name="desktop-shutdown", daemon=True
        ).start()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            restore_handlers.append((signum, signal.getsignal(signum)))
            signal.signal(signum, request_shutdown)

    try:
        LOGGER.info("Desktop service ready on %s:%s", host, server.server_address[1])
        server.serve_forever(poll_interval=0.1)
        return 0
    finally:
        server.server_close()
        for signum, previous in restore_handlers:
            signal.signal(signum, cast_signal_handler(previous))
        LOGGER.info("Desktop service stopped")


def cast_signal_handler(
    handler: signal.Handlers,
) -> Callable[[int, object], None] | int | None:
    return handler
