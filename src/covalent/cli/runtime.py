"""Shared runtime helpers for CLI commands that talk to the database directly."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

T = TypeVar("T")


def load_settings() -> AppSettings:
    settings = AppSettings()
    if not settings.database_url:
        raise SystemExit("AGENT_FRAMEWORK_DATABASE_URL must be set for this command")
    return settings


def run_async(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return asyncio.run(fn(*args, **kwargs))


@contextlib.asynccontextmanager
async def database_session(settings: AppSettings):
    db = DatabaseManager(settings.database_url)
    try:
        yield db
    finally:
        await db.dispose()
