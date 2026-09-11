"""Shared runtime helpers for CLI commands that talk to the database directly."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml

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


def load_config_file(path: str) -> dict[str, Any]:
    """Read a YAML (or JSON — a YAML subset) resource definition; '-' reads stdin."""
    if path == "-":
        text = sys.stdin.read()
    else:
        file = Path(path)
        if not file.exists():
            raise SystemExit(f"Error: file not found: {path}")
        text = file.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"Error: invalid YAML/JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Error: {path} must contain a single YAML/JSON object")
    return data


def yaml_dump(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


@contextlib.asynccontextmanager
async def config_store(settings: AppSettings):
    from covalent.infra.config_store import ConfigStore

    async with database_session(settings) as db:
        yield ConfigStore(db.session_factory)


def _lookup_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    env_file = Path(".env")
    if env_file.exists():
        from dotenv import dotenv_values

        return str(dotenv_values(env_file).get(name) or "")
    return ""


def service_base_url() -> str:
    """Base URL of the running covalent service for /v1 calls."""
    resolved = _lookup_env("COVALENT_BASE_URL").rstrip("/")
    if not resolved:
        resolved = f"http://127.0.0.1:{os.getenv('AGENT_FRAMEWORK_BACKEND_PORT', '5170')}"
    return resolved


class ReloadError(RuntimeError):
    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        super().__init__(detail)


def _reload_detail(response: Any) -> str:
    if response.headers.get("content-type", "").startswith("application/json"):
        return str(response.json().get("detail", response.text))
    return response.text


def reload_running_service(timeout: float = 30.0, base_url: str | None = None) -> dict[str, Any]:
    """POST /v1/ops/reload so the running service picks up DB/disk changes.

    The endpoint requires an admin-owned API token; pass it via the
    ``COVALENT_API_TOKEN`` environment variable. Raises ReloadError on HTTP
    failure and ConnectionError when the service is unreachable.
    """
    import httpx

    token = _lookup_env("COVALENT_API_TOKEN")
    if not token:
        raise SystemExit(
            "COVALENT_API_TOKEN must be set (an admin-owned token) to reload the running service"
        )
    url = f"{(base_url or service_base_url()).rstrip('/')}/v1/ops/reload"
    try:
        response = httpx.post(url, timeout=timeout, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise ConnectionError(str(exc)) from exc
    if response.status_code != 200:
        raise ReloadError(response.status_code, _reload_detail(response))
    return dict(response.json())
