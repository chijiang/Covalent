"""Check the runtime health of a running covalent service."""

from __future__ import annotations

import json
from typing import Any

import httpx
import typer

from covalent.cli.runtime import service_base_url, yaml_dump


def _report(payload: dict[str, Any]) -> int:
    status = str(payload.get("status") or "unknown")
    color = typer.colors.GREEN if status == "ok" else typer.colors.RED
    typer.secho(f"status: {status}", fg=color)
    for key in sorted(payload):
        if key == "status":
            continue
        value = payload[key]
        if isinstance(value, dict):
            typer.echo(yaml_dump({key: value}).rstrip())
        else:
            typer.echo(f"{key}: {value}")
    return 0 if status == "ok" else 1


def check(
    base_url: str | None = typer.Option(
        None, "--base-url", help="Override the service base URL"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the raw /healthz payload as JSON"
    ),
    timeout: float = typer.Option(5.0, "--timeout", help="HTTP timeout in seconds"),
) -> None:
    """Check runtime health via GET /healthz.

    Exit codes: 0 = ok, 1 = degraded, 2 = unreachable or invalid response.
    """
    url = f"{(base_url or service_base_url()).rstrip('/')}/healthz"
    typer.echo(f"checking {url}")
    try:
        response = httpx.get(url, timeout=timeout, trust_env=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    try:
        payload = response.json()
    except ValueError:
        typer.secho(
            f"invalid JSON response: {response.text[:200]}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)
    if not isinstance(payload, dict):
        typer.secho(
            f"unexpected /healthz payload: {payload!r}", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        status = str(payload.get("status") or "unknown")
        raise typer.Exit(code=0 if status == "ok" else 1)
    raise typer.Exit(code=_report(payload))
