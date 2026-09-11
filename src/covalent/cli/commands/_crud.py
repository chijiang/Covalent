"""Generic document CRUD shared by the agent/provider/mcp CLI command groups.

CLI writes go straight to the database via ConfigStore with principal=None
(admin scope) and reuse the console's validation (`_validate_config_payload`)
plus the delegate-run conflict guard, so CLI writes cannot bypass the rules
the service enforces. The running service picks changes up via
`config reload` / `--reload`.
"""

from __future__ import annotations

from typing import Any

import typer
from pydantic import ValidationError

from covalent.application.errors import ConflictError, InvalidInputError
from covalent.application.services.management_service import (
    _validate_config_payload,
    enforce_agent_delegate_run_checks,
)
from covalent.infra.config_store import ConfigKind
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings

_IDENTITY_FIELDS = (
    "internal_name",
    "owner_user_id",
    "workspace_id",
    "visibility",
    "publication_status",
    "publication_requested_at",
    "publication_reviewed_at",
    "publication_reviewed_by_user_id",
)


def _find(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if str(item.get("name") or "") == name:
            return item
    return None


def _validated_or_exit(kind: ConfigKind, payload: list[dict[str, Any]], settings: AppSettings) -> list[dict[str, Any]]:
    try:
        return _validate_config_payload(kind, payload, settings)
    except (InvalidInputError, ValidationError) as exc:
        message = getattr(exc, "message", None) or str(exc)
        typer.secho(f"Error: invalid {kind} configuration: {message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


async def upsert_item(
    db: DatabaseManager,
    kind: ConfigKind,
    name: str,
    data: dict[str, Any],
    settings: AppSettings,
) -> bool:
    """Insert or replace one resource (by public name) in the document.

    Identity metadata (internal_name/owner/workspace/visibility/…) not present
    in the file is preserved from the existing row so `set` never demotes or
    re-owns a resource by accident. Returns True when the resource is new.
    """
    from covalent.infra.config_store import ConfigStore

    store = ConfigStore(db.session_factory)
    document = list(await store.get_document(kind))
    incoming = dict(data)
    incoming["name"] = name
    existing = _find(document, name)
    if existing is not None:
        preserved = {key: existing[key] for key in _IDENTITY_FIELDS if key in existing and key not in incoming}
        incoming = {**preserved, **incoming, "name": name}
    _validated_or_exit(kind, [incoming], settings)
    merged = [item for item in document if str(item.get("name") or "") != name]
    merged.append(incoming)
    try:
        await store.save_document(kind, _validated_or_exit(kind, merged, settings))
    except ConflictError as exc:
        typer.secho(f"Error: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    return existing is None


async def delete_item(
    db: DatabaseManager,
    kind: ConfigKind,
    name: str,
    settings: AppSettings,
    *,
    force: bool = False,
    check_delegates: bool = False,
) -> None:
    from covalent.infra.config_store import ConfigStore
    from covalent.infra.delegate_repository import PostgresDelegateRunStore

    store = ConfigStore(db.session_factory)
    document = list(await store.get_document(kind))
    if _find(document, name) is None:
        typer.secho(f"Error: no {kind[:-1] if kind.endswith('s') else kind} named '{name}'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    remaining = [item for item in document if str(item.get("name") or "") != name]
    if check_delegates and not force:
        run_store = PostgresDelegateRunStore(db.session_factory)
        try:
            await enforce_agent_delegate_run_checks(
                run_store,
                payload_names={str(item.get("name") or "") for item in remaining},
                current_document=document,
                renamed_from=set(),
            )
        except ConflictError as exc:
            typer.secho(
                f"Error: {exc.message}\nUse --force to remove anyway.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2) from exc
    await store.save_document(kind, _validated_or_exit(kind, remaining, settings))


async def load_document(db: DatabaseManager, kind: ConfigKind) -> list[dict[str, Any]]:
    from covalent.infra.config_store import ConfigStore

    store = ConfigStore(db.session_factory)
    return list(await store.get_document(kind))


def apply_or_hint(reload_requested: bool) -> None:
    """After a write: hot-reload the running service, or tell the user how."""
    from covalent.cli.runtime import ReloadError, reload_running_service

    if reload_requested:
        try:
            summary = reload_running_service()
        except ConnectionError as exc:
            typer.secho(
                f"Note: service unreachable ({exc}); change is saved and applies on next start.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            return
        except ReloadError as exc:
            typer.secho(f"Note: reload failed ({exc}); change is saved in the database.", fg=typer.colors.YELLOW, err=True)
            return
        typer.echo(
            "Applied to running service: agents=[{agents}] mcp={mcp} skills={skills}".format(
                agents=", ".join(summary.get("agents", [])),
                mcp=summary.get("mcp_servers"),
                skills=summary.get("manifest_skills"),
            )
        )
        return
    typer.secho(
        "Note: run `config reload` (or restart) so the running service picks up this change.",
        fg=typer.colors.CYAN,
        err=True,
    )
