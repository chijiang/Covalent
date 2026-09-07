"""Export/import the full platform configuration as a single zip bundle.

A bundle zip contains ``config.yaml`` (all configuration tables, with
natural-key references: workspace slug / user email) plus local skill files
under ``skills/<category>/<skill-name>/``. Import is a per-row upsert driven by
natural keys so it never deletes rows that are absent from the bundle (unlike
``ConfigStore.save_document`` which fully replaces a kind).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any
import zipfile

import yaml
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from covalent.application.services.config_bundle_schema import (
    BUNDLE_KIND,
    BUNDLED_SKILL_CATEGORIES,
    SCHEMA_VERSION,
    ConfigBundle,
    SandboxProfileEntry,
    SkillStateEntry,
    UserEntry,
    WorkspaceEntry,
    WorkspaceMemberEntry,
    validate_schema_version,
)
from covalent.infra.config_store import (
    ConfigStore,
    PersistedAgentConfig,
    PersistedMcpServerMetadata,
    PersistedProviderConfig,
    PersistedSkillSourceConfig,
)
from covalent.infra.db import (
    AgentCapabilityRow,
    AgentDelegateRow,
    AgentMcpServerRow,
    AgentMcpToolRow,
    AgentRow,
    AgentSkillRow,
    DatabaseManager,
    McpServerEnvVarRow,
    McpServerRow,
    ProviderRow,
    SandboxProfileRow,
    SkillSourceRow,
    SkillStateRow,
    UserRow,
    WorkspaceMemberRow,
    WorkspaceRow,
)
from covalent.mcp.spec import McpServerConfig
from covalent.skills.loader import SkillLoader

BUNDLE_CONFIG_NAME = "config.yaml"
BUNDLE_SKILLS_PREFIX = "skills/"

_SKILL_FILE_EXCLUDED_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}


def _bundle_generator() -> str:
    try:
        from importlib.metadata import version

        return f"covalent {version('agent-framework')}"
    except Exception:
        return "covalent"


@dataclass
class KindReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


@dataclass
class ImportReport:
    kinds: dict[str, KindReport] = field(default_factory=dict)
    skills_installed: int = 0
    skills_replaced: int = 0
    skills_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def kind(self, name: str) -> KindReport:
        return self.kinds.setdefault(name, KindReport())


@dataclass
class _ValidatedItems:
    providers: list[tuple[str, PersistedProviderConfig]] = field(default_factory=list)
    mcp_servers: list[tuple[str, tuple[McpServerConfig, PersistedMcpServerMetadata]]] = field(default_factory=list)
    skill_sources: list[tuple[str, PersistedSkillSourceConfig]] = field(default_factory=list)
    agents: list[tuple[str, PersistedAgentConfig]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def _translate_resource_refs(
    item: dict[str, Any],
    *,
    user_email_by_id: dict[str, str],
    workspace_slug_by_id: dict[str, str],
    warnings: list[str],
    label: str,
) -> dict[str, Any]:
    translated = dict(item)
    owner_id = translated.pop("owner_user_id", None)
    workspace_id = translated.pop("workspace_id", None)
    reviewer_id = translated.pop("publication_reviewed_by_user_id", None)
    if owner_id:
        translated["owner_email"] = user_email_by_id.get(owner_id)
        if translated["owner_email"] is None:
            warnings.append(f"{label}: owner user id {owner_id} not found; exported as null")
    if workspace_id:
        translated["workspace_slug"] = workspace_slug_by_id.get(workspace_id)
        if translated["workspace_slug"] is None:
            warnings.append(f"{label}: workspace id {workspace_id} not found; exported as null")
    if reviewer_id:
        translated["reviewed_by_email"] = user_email_by_id.get(reviewer_id)
    return translated


def _collect_skill_files(settings, include_skills: bool, warnings: list[str]) -> list[tuple[Path, str]]:
    if not include_skills:
        return []
    from covalent.application.services.skill_service import _skill_category

    files: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for spec in sorted(SkillLoader(settings).discover_local(), key=lambda s: s.name):
        category = _skill_category(spec, settings)
        if category not in BUNDLED_SKILL_CATEGORIES:
            continue
        source_dir = Path(spec.source_dir or "")
        if not source_dir.is_dir():
            continue
        arc_root = f"{BUNDLE_SKILLS_PREFIX}{category}/{source_dir.name}"
        if arc_root in seen:
            warnings.append(f"skills: duplicate skill directory '{arc_root}'; keeping the first occurrence")
            continue
        seen.add(arc_root)
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if _SKILL_FILE_EXCLUDED_DIRS.intersection(file_path.parts) or file_path.suffix == ".pyc":
                continue
            files.append((file_path, (Path(arc_root) / file_path.relative_to(source_dir)).as_posix()))
    return files


async def export_bundle(settings, output: Path, *, include_skills: bool = True) -> dict[str, Any]:
    """Write a full configuration bundle zip to ``output``. Returns a summary."""
    output = Path(output)
    warnings: list[str] = []

    db = DatabaseManager(settings.database_url)
    try:
        async with db.session_factory() as session:
            workspace_rows = list(
                await session.scalars(select(WorkspaceRow).order_by(WorkspaceRow.slug))
            )
            user_rows = list(await session.scalars(select(UserRow).order_by(UserRow.email)))
            member_rows = list(
                await session.scalars(select(WorkspaceMemberRow).order_by(WorkspaceMemberRow.workspace_id))
            )
            profile_rows = list(
                await session.scalars(select(SandboxProfileRow).order_by(SandboxProfileRow.id))
            )
            skill_state_rows = list(
                await session.scalars(select(SkillStateRow).order_by(SkillStateRow.skill_name))
            )
            agent_provider_keys = {
                row.name: row.provider_api_key for row in await session.scalars(select(AgentRow))
            }

        store = ConfigStore(db.session_factory)
        providers = await store.get_document("providers", None)
        mcp_servers = await store.get_document("mcp", None)
        skill_sources = await store.get_document("skill_sources", None)
        agents = await store.get_document("agents", None)
    finally:
        await db.dispose()

    user_email_by_id = {row.id: row.email for row in user_rows}
    workspace_slug_by_id = {row.id: row.slug for row in workspace_rows}

    def translate(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        return [
            _translate_resource_refs(
                item,
                user_email_by_id=user_email_by_id,
                workspace_slug_by_id=workspace_slug_by_id,
                warnings=warnings,
                label=f"{kind}[{item.get('name', '?')}]",
            )
            for item in items
        ]

    providers = translate(providers, "providers")
    mcp_servers = translate(mcp_servers, "mcp_servers")
    skill_sources = translate(skill_sources, "skill_sources")
    agents = translate(agents, "agents")
    # ProviderConfig.api_key is excluded from model_dump, so the agent-level
    # provider key must be re-injected from the raw row.
    for item in agents:
        internal_name = item.get("internal_name") or item.get("name")
        provider = dict(item.get("provider") or {})
        provider["api_key"] = agent_provider_keys.get(internal_name)
        item["provider"] = provider

    member_entries = [
        {
            "workspace": workspace_slug_by_id.get(row.workspace_id),
            "user_email": user_email_by_id.get(row.user_id),
            "role": row.role,
        }
        for row in sorted(
            member_rows,
            key=lambda r: (
                workspace_slug_by_id.get(r.workspace_id, r.workspace_id),
                user_email_by_id.get(r.user_id, r.user_id),
            ),
        )
    ]

    bundle = ConfigBundle(
        metadata={
            "kind": BUNDLE_KIND,
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC),
            "generator": _bundle_generator(),
        },
        workspaces=[
            WorkspaceEntry(legacy_id=row.id, slug=row.slug, name=row.name)
            for row in workspace_rows
        ],
        users=[
            UserEntry(
                legacy_id=row.id,
                email=row.email,
                username=row.username,
                display_name=row.display_name,
                avatar_url=row.avatar_url,
                preferences=row.preferences_json or {},
                password_hash=row.password_hash,
                role=row.role,
                status=row.status,
                auth_subject=row.auth_subject,
            )
            for row in user_rows
        ],
        workspace_members=member_entries,
        sandbox_profiles=[
            SandboxProfileEntry(
                id=row.id,
                name=row.name,
                image=row.image,
                keepalive_command=row.keepalive_command or [],
                memory_limit=row.memory_limit,
                pids_limit=row.pids_limit,
                cpus=row.cpus,
                tmpfs_size=row.tmpfs_size,
                description=row.description,
                workspace=workspace_slug_by_id.get(row.workspace_id),
                pull_policy=row.pull_policy,
                runtime_capabilities=row.runtime_capabilities or [],
                contract_version=row.contract_version,
                enabled=row.enabled,
                is_default=row.is_default,
                revision=row.revision,
                validation_status=row.validation_status,
                validated_image_id=row.validated_image_id,
                validated_image_digest=row.validated_image_digest,
                validated_at=row.validated_at,
                validation_message=row.validation_message,
            )
            for row in profile_rows
        ],
        providers=providers,
        mcp_servers=mcp_servers,
        skill_sources=skill_sources,
        skill_states=[
            SkillStateEntry(skill_name=row.skill_name, enabled=row.enabled)
            for row in skill_state_rows
        ],
        agents=agents,
    )

    skill_files = _collect_skill_files(settings, include_skills, warnings)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            BUNDLE_CONFIG_NAME,
            yaml.safe_dump(bundle.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        )
        for file_path, arcname in skill_files:
            archive.write(file_path, arcname)

    return {
        "output": str(output),
        "workspaces": len(bundle.workspaces),
        "users": len(bundle.users),
        "workspace_members": len(bundle.workspace_members),
        "sandbox_profiles": len(bundle.sandbox_profiles),
        "providers": len(bundle.providers),
        "mcp_servers": len(bundle.mcp_servers),
        "skill_sources": len(bundle.skill_sources),
        "skill_states": len(bundle.skill_states),
        "agents": len(bundle.agents),
        "skill_files": len(skill_files),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Import — shared resolution / validation
# --------------------------------------------------------------------------


def read_bundle(bundle: Path) -> tuple[ConfigBundle, list[str], list[str]]:
    """Parse a bundle zip. Returns (bundle, skill_arcnames, schema_warnings)."""
    bundle = Path(bundle)
    if not bundle.is_file():
        raise ValueError(f"Bundle file not found: {bundle}")
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if BUNDLE_CONFIG_NAME not in names:
            raise ValueError(f"Bundle is missing {BUNDLE_CONFIG_NAME}")
        raw = yaml.safe_load(archive.read(BUNDLE_CONFIG_NAME))
        skill_arcnames = [name for name in names if name.startswith(BUNDLE_SKILLS_PREFIX)]
    if not isinstance(raw, dict):
        raise ValueError("Bundle config must be a YAML mapping")
    metadata = raw.get("metadata") or {}
    if metadata.get("kind") != BUNDLE_KIND:
        raise ValueError(
            f"Not a covalent config bundle (metadata.kind={metadata.get('kind')!r}, expected {BUNDLE_KIND!r})"
        )
    warnings = validate_schema_version(int(metadata.get("schema_version") or 0))
    parsed = ConfigBundle.model_validate(raw)
    return parsed, skill_arcnames, warnings


async def _load_context(session: AsyncSession) -> dict[str, Any]:
    user_rows = list(await session.scalars(select(UserRow)))
    workspace_rows = list(await session.scalars(select(WorkspaceRow)))
    agent_rows = list(await session.scalars(select(AgentRow)))
    mcp_rows = list(await session.scalars(select(McpServerRow)))
    return {
        "user_id_by_email": {row.email.lower(): row.id for row in user_rows},
        "user_id_by_username": {row.username.lower(): row.id for row in user_rows},
        "workspace_id_by_slug": {row.slug: row.id for row in workspace_rows},
        "agent_internal_by_name": {
            name: row.name for row in agent_rows for name in {row.name, row.display_name or row.name}
        },
        "mcp_internal_by_name": {
            name: row.name for row in mcp_rows for name in {row.name, row.display_name or row.name}
        },
    }


def _resolve_item_refs(
    item: dict[str, Any],
    context: dict[str, Any],
    warnings: list[str],
    label: str,
) -> dict[str, Any]:
    resolved = dict(item)
    owner_email = resolved.pop("owner_email", None)
    workspace_slug = resolved.pop("workspace_slug", None)
    reviewer_email = resolved.pop("reviewed_by_email", None)
    owner_id = None
    if owner_email:
        owner_id = context["user_id_by_email"].get(str(owner_email).lower())
        if owner_id is None:
            warnings.append(f"{label}: owner '{owner_email}' not found on target; owner cleared")
    workspace_id = None
    if workspace_slug:
        workspace_id = context["workspace_id_by_slug"].get(workspace_slug)
        if workspace_id is None:
            warnings.append(f"{label}: workspace '{workspace_slug}' not found on target; workspace cleared")
    reviewer_id = None
    if reviewer_email:
        reviewer_id = context["user_id_by_email"].get(str(reviewer_email).lower())
    resolved["owner_user_id"] = owner_id
    resolved["workspace_id"] = workspace_id
    resolved["publication_reviewed_by_user_id"] = reviewer_id
    return resolved


def _build_validated_items(
    bundle: ConfigBundle,
    context: dict[str, Any],
    warnings: list[str],
) -> _ValidatedItems:
    items = _ValidatedItems()

    for item in bundle.providers:
        label = f"providers[{item.get('name')}]"
        resolved = _resolve_item_refs(item, context, warnings, label)
        items.providers.append((label, _catch(PersistedProviderConfig, resolved, label)))

    for item in bundle.mcp_servers:
        label = f"mcp_servers[{item.get('name')}]"
        resolved = _resolve_item_refs(item, context, warnings, label)
        items.mcp_servers.append(
            (
                label,
                (
                    _catch(McpServerConfig, resolved, label),
                    _catch(PersistedMcpServerMetadata, resolved, label),
                ),
            )
        )

    for index, item in enumerate(bundle.skill_sources):
        label = f"skill_sources[{item.get('name') or index}]"
        resolved = _resolve_item_refs(item, context, warnings, label)
        items.skill_sources.append((label, _catch(PersistedSkillSourceConfig, resolved, label)))

    for item in bundle.agents:
        label = f"agents[{item.get('name')}]"
        resolved = _resolve_item_refs(item, context, warnings, label)
        items.agents.append((label, _catch(PersistedAgentConfig, resolved, label)))

    return items


def _catch(model: type, payload: dict[str, Any], label: str) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"{label}: invalid item: {exc}") from exc


def _validate_agent_relations(
    items: _ValidatedItems,
    context: dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Validate cross references. Returns {'agent': public->internal, 'mcp': public->internal}."""
    agent_map = dict(context["agent_internal_by_name"])
    for _label, config in items.agents:
        agent_map[config.name] = config.internal_name or config.name
    mcp_map = dict(context["mcp_internal_by_name"])
    for _label, (server, metadata) in items.mcp_servers:
        mcp_map[server.name] = metadata.internal_name or server.name

    bundle_agent_names = {config.name for _label, config in items.agents}
    for label, config in items.agents:
        for delegate in config.delegate_agents:
            if delegate not in bundle_agent_names and delegate not in agent_map:
                raise ValueError(f"{label}: unknown delegate agent '{delegate}'")
        for server in config.mcp_servers:
            if server not in mcp_map:
                raise ValueError(f"{label}: unknown MCP server '{server}'")
        for tool in config.mcp_tools:
            if tool.server_name not in mcp_map:
                raise ValueError(f"{label}: MCP tool '{tool.tool_name}' references unknown server '{tool.server_name}'")
            if tool.server_name not in config.mcp_servers:
                raise ValueError(
                    f"{label}: MCP tool '{tool.tool_name}' references unselected server '{tool.server_name}'"
                )
    return {"agent": agent_map, "mcp": mcp_map}


async def _check_sandbox_profiles(
    session: AsyncSession,
    items: _ValidatedItems,
    report: ImportReport,
    *,
    strict: bool,
) -> set[str]:
    existing_ids = set(await session.scalars(select(SandboxProfileRow.id)))
    for label, config in items.agents:
        if config.sandbox_profile_id and config.sandbox_profile_id not in existing_ids:
            message = f"{label}: sandbox profile '{config.sandbox_profile_id}' not found on target"
            if strict:
                raise ValueError(message)
            # Clearing avoids an FK violation against the missing profile; the
            # agent falls back to the platform default profile.
            config.sandbox_profile_id = None
            report.warnings.append(message + "; sandbox profile cleared")
    return existing_ids


def _enforce_strict(warnings: list[str], *, strict: bool) -> None:
    if not strict:
        return
    degraded = [w for w in warnings if "cleared" in w]
    if degraded:
        raise ValueError("Strict import aborted due to unresolved references:\n" + "\n".join(degraded))


# --------------------------------------------------------------------------
# Import — apply (single transaction)
# --------------------------------------------------------------------------


async def _upsert_workspaces(
    session: AsyncSession,
    entries: list[WorkspaceEntry],
    context: dict[str, Any],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("workspaces")
    for entry in entries:
        existing_id = context["workspace_id_by_slug"].get(entry.slug)
        if existing_id is not None:
            if on_conflict == "skip":
                rep.skipped += 1
                continue
            row = await session.get(WorkspaceRow, existing_id)
            row.name = entry.name
            rep.updated += 1
        else:
            session.add(WorkspaceRow(id=entry.legacy_id, slug=entry.slug, name=entry.name))
            context["workspace_id_by_slug"][entry.slug] = entry.legacy_id
            rep.inserted += 1


async def _upsert_users(
    session: AsyncSession,
    entries: list[UserEntry],
    context: dict[str, Any],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("users")
    for entry in entries:
        user_id = context["user_id_by_email"].get(entry.email.lower())
        if user_id is None:
            user_id = context["user_id_by_username"].get(entry.username.lower())
            if user_id is not None:
                report.warnings.append(
                    f"users[{entry.email}]: email not found; matched existing user by username '{entry.username}'"
                )
        if user_id is not None:
            if on_conflict == "skip":
                rep.skipped += 1
                continue
            row = await session.get(UserRow, user_id)
            row.username = entry.username
            row.display_name = entry.display_name
            row.avatar_url = entry.avatar_url
            row.preferences_json = dict(entry.preferences)
            if entry.password_hash is not None:
                row.password_hash = entry.password_hash
            row.role = entry.role
            row.status = entry.status
            if entry.auth_subject is not None:
                row.auth_subject = entry.auth_subject
            rep.updated += 1
        else:
            session.add(
                UserRow(
                    id=entry.legacy_id,
                    email=entry.email,
                    username=entry.username,
                    display_name=entry.display_name,
                    avatar_url=entry.avatar_url,
                    preferences_json=dict(entry.preferences),
                    password_hash=entry.password_hash,
                    role=entry.role,
                    status=entry.status,
                    auth_subject=entry.auth_subject,
                )
            )
            rep.inserted += 1
        context["user_id_by_email"][entry.email.lower()] = user_id or entry.legacy_id
        context["user_id_by_username"][entry.username.lower()] = user_id or entry.legacy_id


async def _upsert_workspace_members(
    session: AsyncSession,
    entries: list[WorkspaceMemberEntry],
    context: dict[str, Any],
    report: ImportReport,
) -> None:
    rep = report.kind("workspace_members")
    for entry in entries:
        workspace_id = context["workspace_id_by_slug"].get(entry.workspace)
        user_id = context["user_id_by_email"].get(entry.user_email.lower())
        if workspace_id is None or user_id is None:
            report.warnings.append(
                f"workspace_members: skipped '{entry.user_email}' in '{entry.workspace}' (user or workspace missing)"
            )
            continue
        row = await session.get(WorkspaceMemberRow, (workspace_id, user_id))
        if row is not None:
            row.role = entry.role
            rep.updated += 1
        else:
            session.add(WorkspaceMemberRow(workspace_id=workspace_id, user_id=user_id, role=entry.role))
            rep.inserted += 1


async def _upsert_sandbox_profiles(
    session: AsyncSession,
    entries: list[SandboxProfileEntry],
    context: dict[str, Any],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("sandbox_profiles")
    for entry in entries:
        row = await session.get(SandboxProfileRow, entry.id)
        values: dict[str, Any] = {
            "name": entry.name,
            "description": entry.description,
            "image": entry.image,
            "pull_policy": entry.pull_policy,
            "keepalive_command": list(entry.keepalive_command),
            "runtime_capabilities": list(entry.runtime_capabilities),
            "contract_version": entry.contract_version,
            "memory_limit": entry.memory_limit,
            "pids_limit": entry.pids_limit,
            "cpus": entry.cpus,
            "tmpfs_size": entry.tmpfs_size,
            "enabled": entry.enabled,
            "is_default": entry.is_default,
            "revision": entry.revision,
            "validation_status": entry.validation_status,
            "validated_image_id": entry.validated_image_id,
            "validated_image_digest": entry.validated_image_digest,
            "validated_at": entry.validated_at,
            "validation_message": entry.validation_message,
            "workspace_id": context["workspace_id_by_slug"].get(entry.workspace) if entry.workspace else None,
        }
        if entry.workspace and values["workspace_id"] is None:
            report.warnings.append(
                f"sandbox_profiles[{entry.id}]: workspace '{entry.workspace}' not found on target; workspace cleared"
            )
        if row is None:
            session.add(SandboxProfileRow(id=entry.id, **values))
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
        else:
            for key, value in values.items():
                setattr(row, key, value)
            rep.updated += 1


async def _upsert_providers(
    session: AsyncSession,
    items: list[tuple[str, PersistedProviderConfig]],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("providers")
    for _label, config in items:
        internal = config.internal_name or config.name
        row = (
            await session.scalars(select(ProviderRow).where(ProviderRow.name == internal))
        ).first()
        if row is None:
            row = (
                await session.scalars(select(ProviderRow).where(ProviderRow.display_name == config.name))
            ).first()
        if row is None:
            row = ProviderRow(name=internal)
            session.add(row)
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
            continue
        else:
            rep.updated += 1
        row.display_name = config.name if config.name != row.name else None
        row.provider_type = config.provider_type
        row.base_url = config.base_url
        row.api_key = config.api_key
        row.apih_config = config.apih.model_dump(mode="json") if config.apih else None
        row.default_model = config.default_model
        row.is_default = config.is_default
        row.position = config.position
        row.owner_user_id = config.owner_user_id
        row.workspace_id = config.workspace_id
        row.visibility = config.visibility
        row.publication_status = config.publication_status
        row.publication_requested_at = config.publication_requested_at
        row.publication_reviewed_at = config.publication_reviewed_at
        row.publication_reviewed_by_user_id = config.publication_reviewed_by_user_id


async def _upsert_mcp_servers(
    session: AsyncSession,
    items: list[tuple[str, tuple[McpServerConfig, PersistedMcpServerMetadata]]],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("mcp_servers")
    for position, (_label, (server, metadata)) in enumerate(items):
        internal = metadata.internal_name or server.name
        row = (
            await session.scalars(select(McpServerRow).where(McpServerRow.name == internal))
        ).first()
        if row is None:
            row = (
                await session.scalars(select(McpServerRow).where(McpServerRow.display_name == server.name))
            ).first()
        if row is None:
            row = McpServerRow(name=internal)
            session.add(row)
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
            continue
        else:
            rep.updated += 1
        row.display_name = server.name if server.name != row.name else None
        row.position = position
        row.transport = server.transport
        row.command = server.command
        row.args = list(server.args)
        row.url = server.url
        row.owner_user_id = metadata.owner_user_id
        row.workspace_id = metadata.workspace_id
        row.visibility = metadata.visibility
        row.publication_status = metadata.publication_status
        row.publication_requested_at = metadata.publication_requested_at
        row.publication_reviewed_at = metadata.publication_reviewed_at
        row.publication_reviewed_by_user_id = metadata.publication_reviewed_by_user_id

        await session.execute(delete(McpServerEnvVarRow).where(McpServerEnvVarRow.server_name == row.name))
        for key, value in sorted((server.env or {}).items()):
            session.add(McpServerEnvVarRow(server_name=row.name, key=key, value=value))


async def _upsert_skill_sources(
    session: AsyncSession,
    items: list[tuple[str, PersistedSkillSourceConfig]],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("skill_sources")
    for position, (_label, config) in enumerate(items):
        row = (
            await session.scalars(
                select(SkillSourceRow).where(
                    SkillSourceRow.url == config.url,
                    SkillSourceRow.ref == config.ref,
                    SkillSourceRow.subdir == config.subdir,
                    SkillSourceRow.name == config.name,
                )
            )
        ).first()
        if row is None:
            row = SkillSourceRow(position=position)
            session.add(row)
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
            continue
        else:
            rep.updated += 1
        row.position = position
        row.source_type = config.source_type
        row.category = config.category
        row.name = config.name
        row.url = config.url
        row.ref = config.ref
        row.subdir = config.subdir
        row.owner_user_id = config.owner_user_id
        row.workspace_id = config.workspace_id
        row.visibility = config.visibility
        row.publication_status = config.publication_status
        row.publication_requested_at = config.publication_requested_at
        row.publication_reviewed_at = config.publication_reviewed_at
        row.publication_reviewed_by_user_id = config.publication_reviewed_by_user_id


async def _upsert_skill_states(
    session: AsyncSession,
    entries: list[SkillStateEntry],
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    rep = report.kind("skill_states")
    for entry in entries:
        row = await session.get(SkillStateRow, entry.skill_name)
        if row is None:
            session.add(SkillStateRow(skill_name=entry.skill_name, enabled=entry.enabled))
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
        else:
            row.enabled = entry.enabled
            rep.updated += 1


async def _upsert_agents(
    session: AsyncSession,
    items: _ValidatedItems,
    name_maps: dict[str, dict[str, str]],
    report: ImportReport,
    *,
    on_conflict: str,
    strict: bool,
) -> None:
    rep = report.kind("agents")
    agent_map = name_maps["agent"]
    mcp_map = name_maps["mcp"]
    await _check_sandbox_profiles(session, items, report, strict=strict)

    processed_internal_names: list[str] = []
    internal_by_label: dict[str, str] = {}
    for position, (_label, config) in enumerate(items.agents):
        requested_internal = config.internal_name or config.name
        row = (await session.scalars(select(AgentRow).where(AgentRow.name == requested_internal))).first()
        if row is None:
            row = (await session.scalars(select(AgentRow).where(AgentRow.display_name == config.name))).first()
        if row is None:
            row = AgentRow(name=requested_internal)
            session.add(row)
            rep.inserted += 1
        elif on_conflict == "skip":
            rep.skipped += 1
            continue
        else:
            rep.updated += 1
        # Relations must key on the row's actual PK, which may differ from the
        # requested internal name when matched via display name.
        internal_by_label[_label] = row.name
        processed_internal_names.append(row.name)

        row.display_name = config.name if config.name != row.name else None
        row.position = position
        row.enabled = config.enabled
        row.description = config.description
        row.system_prompt = config.system_prompt
        row.reasoning_prompt = config.reasoning_prompt
        row.reasoning_level = config.reasoning_level
        row.local_tools = list(config.local_tools)
        row.allowed_outbound = list(config.allowed_outbound)
        row.provider_name = config.provider.provider
        row.provider_model = config.provider.model
        row.provider_api_key = config.provider.api_key
        row.provider_base_url = config.provider.base_url
        row.provider_timeout_seconds = config.provider.timeout_seconds
        row.provider_extra = dict(config.provider.extra)
        row.max_iterations = config.max_iterations
        row.context_window = config.context_window
        row.metadata_json = dict(config.metadata)
        row.sandbox_profile_id = config.sandbox_profile_id
        row.owner_user_id = config.owner_user_id
        row.workspace_id = config.workspace_id
        row.visibility = config.visibility
        row.publication_status = config.publication_status
        row.publication_requested_at = config.publication_requested_at
        row.publication_reviewed_at = config.publication_reviewed_at
        row.publication_reviewed_by_user_id = config.publication_reviewed_by_user_id

    await session.flush()

    if processed_internal_names:
        await session.execute(
            delete(AgentCapabilityRow).where(AgentCapabilityRow.agent_name.in_(processed_internal_names))
        )
        await session.execute(delete(AgentSkillRow).where(AgentSkillRow.agent_name.in_(processed_internal_names)))
        await session.execute(delete(AgentDelegateRow).where(AgentDelegateRow.agent_name.in_(processed_internal_names)))
        await session.execute(
            delete(AgentMcpServerRow).where(AgentMcpServerRow.agent_name.in_(processed_internal_names))
        )
        await session.execute(delete(AgentMcpToolRow).where(AgentMcpToolRow.agent_name.in_(processed_internal_names)))

    for label, config in items.agents:
        internal = internal_by_label.get(label)
        if internal is None:
            continue
        for position, capability in enumerate(sorted(config.capabilities, key=lambda c: c.value)):
            session.add(
                AgentCapabilityRow(agent_name=internal, capability=capability.value, position=position)
            )
        for position, skill_name in enumerate(config.skills):
            session.add(AgentSkillRow(agent_name=internal, skill_name=skill_name, position=position))
        for position, delegate in enumerate(config.delegate_agents):
            session.add(
                AgentDelegateRow(
                    agent_name=internal,
                    delegate_agent_name=agent_map.get(delegate, delegate),
                    position=position,
                )
            )
        for position, server in enumerate(config.mcp_servers):
            session.add(
                AgentMcpServerRow(agent_name=internal, server_name=mcp_map.get(server, server), position=position)
            )
        for position, tool in enumerate(config.mcp_tools):
            session.add(
                AgentMcpToolRow(
                    agent_name=internal,
                    server_name=mcp_map.get(tool.server_name, tool.server_name),
                    tool_name=tool.tool_name,
                    position=position,
                )
            )


async def _apply_import(
    session: AsyncSession,
    bundle: ConfigBundle,
    report: ImportReport,
    *,
    on_conflict: str,
    strict: bool,
) -> None:
    context = await _load_context(session)
    await _upsert_workspaces(session, bundle.workspaces, context, report, on_conflict=on_conflict)
    await _upsert_users(session, bundle.users, context, report, on_conflict=on_conflict)
    await _upsert_workspace_members(session, bundle.workspace_members, context, report)
    await _upsert_sandbox_profiles(session, bundle.sandbox_profiles, context, report, on_conflict=on_conflict)

    items = _build_validated_items(bundle, context, report.warnings)
    name_maps = _validate_agent_relations(items, context)
    _enforce_strict(report.warnings, strict=strict)

    await _upsert_providers(session, items.providers, report, on_conflict=on_conflict)
    await _upsert_mcp_servers(session, items.mcp_servers, report, on_conflict=on_conflict)
    await _upsert_skill_sources(session, items.skill_sources, report, on_conflict=on_conflict)
    await _upsert_skill_states(session, bundle.skill_states, report, on_conflict=on_conflict)
    await _upsert_agents(session, items, name_maps, report, on_conflict=on_conflict, strict=strict)


# --------------------------------------------------------------------------
# Import — plan (dry run)
# --------------------------------------------------------------------------


async def _plan_import(
    session: AsyncSession,
    bundle: ConfigBundle,
    report: ImportReport,
    *,
    strict: bool,
) -> None:
    context = await _load_context(session)

    rep = report.kind("workspaces")
    for entry in bundle.workspaces:
        if entry.slug in context["workspace_id_by_slug"]:
            rep.updated += 1
        else:
            rep.inserted += 1

    rep = report.kind("users")
    for entry in bundle.users:
        found = entry.email.lower() in context["user_id_by_email"] or entry.username.lower() in context[
            "user_id_by_username"
        ]
        if found:
            rep.updated += 1
        else:
            rep.inserted += 1

    rep = report.kind("workspace_members")
    for entry in bundle.workspace_members:
        workspace_id = context["workspace_id_by_slug"].get(entry.workspace)
        user_id = context["user_id_by_email"].get(entry.user_email.lower())
        if workspace_id is None or user_id is None:
            report.warnings.append(
                f"workspace_members: skipped '{entry.user_email}' in '{entry.workspace}' (user or workspace missing)"
            )
            continue
        row = await session.get(WorkspaceMemberRow, (workspace_id, user_id))
        if row is None:
            rep.inserted += 1
        else:
            rep.updated += 1

    rep = report.kind("sandbox_profiles")
    for entry in bundle.sandbox_profiles:
        if await session.get(SandboxProfileRow, entry.id) is None:
            rep.inserted += 1
        else:
            rep.updated += 1

    items = _build_validated_items(bundle, context, report.warnings)
    _validate_agent_relations(items, context)
    await _check_sandbox_profiles(session, items, report, strict=strict)
    _enforce_strict(report.warnings, strict=strict)

    provider_names = {
        name
        for row in await session.scalars(select(ProviderRow))
        for name in {row.name, row.display_name or row.name}
    }
    rep = report.kind("providers")
    for _label, config in items.providers:
        if (config.internal_name or config.name) in provider_names or config.name in provider_names:
            rep.updated += 1
        else:
            rep.inserted += 1

    mcp_names = set(context["mcp_internal_by_name"])
    rep = report.kind("mcp_servers")
    for _label, (server, metadata) in items.mcp_servers:
        if (metadata.internal_name or server.name) in mcp_names or server.name in mcp_names:
            rep.updated += 1
        else:
            rep.inserted += 1

    rep = report.kind("skill_sources")
    existing_sources = {
        (row.url, row.ref, row.subdir, row.name)
        for row in await session.scalars(select(SkillSourceRow))
    }
    for _label, config in items.skill_sources:
        if (config.url, config.ref, config.subdir, config.name) in existing_sources:
            rep.updated += 1
        else:
            rep.inserted += 1

    rep = report.kind("skill_states")
    existing_states = set(await session.scalars(select(SkillStateRow.skill_name)))
    for entry in bundle.skill_states:
        if entry.skill_name in existing_states:
            rep.updated += 1
        else:
            rep.inserted += 1

    rep = report.kind("agents")
    agent_names = set(context["agent_internal_by_name"])
    for _label, config in items.agents:
        if (config.internal_name or config.name) in agent_names or config.name in agent_names:
            rep.updated += 1
        else:
            rep.inserted += 1


async def import_bundle(
    settings,
    bundle_path: Path,
    *,
    on_conflict: str = "overwrite",
    dry_run: bool = False,
    strict: bool = False,
    include_skills: bool = True,
) -> ImportReport:
    if on_conflict not in {"overwrite", "skip"}:
        raise ValueError(f"Invalid on_conflict: {on_conflict}")

    bundle, skill_arcnames, schema_warnings = read_bundle(bundle_path)
    report = ImportReport(dry_run=dry_run)
    report.warnings.extend(schema_warnings)
    db = DatabaseManager(settings.database_url)
    try:
        async with db.session_factory() as session:
            if dry_run:
                await _plan_import(session, bundle, report, strict=strict)
            else:
                async with session.begin():
                    await _apply_import(session, bundle, report, on_conflict=on_conflict, strict=strict)
    except IntegrityError as exc:
        raise ValueError(f"Import failed with a database constraint violation: {_constraint_hint(exc)}") from exc
    finally:
        await db.dispose()

    if include_skills and not dry_run:
        _install_bundle_skills(settings, skill_arcnames, Path(bundle_path), report, on_conflict=on_conflict)
    return report


def _constraint_hint(exc: IntegrityError) -> str:
    detail = str(getattr(exc, "orig", exc))[:300]
    if "auth_subject" in detail:
        return detail + " — a bundle user's auth_subject collides with an existing account"
    if "uq_sandbox_profiles" in detail:
        return detail + " — two sandbox profiles share (workspace, name)"
    return detail


def _safe_relpath(arcname: str) -> Path | None:
    pure = PurePosixPath(arcname)
    parts = pure.parts
    if not parts or pure.is_absolute() or ".." in parts:
        return None
    return Path(*parts)


def _install_bundle_skills(
    settings,
    skill_arcnames: list[str],
    bundle_path: Path,
    report: ImportReport,
    *,
    on_conflict: str,
) -> None:
    if not skill_arcnames:
        return
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="covalent-bundle-") as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(bundle_path) as archive:
            for arcname in skill_arcnames:
                relative = _safe_relpath(arcname)
                if relative is None:
                    report.warnings.append(f"skills: skipped unsafe archive path '{arcname}'")
                    continue
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(arcname))

        skills_root = staging / BUNDLE_SKILLS_PREFIX.rstrip("/")
        if not skills_root.is_dir():
            return
        for category_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
            category = category_dir.name
            if category not in BUNDLED_SKILL_CATEGORIES:
                report.warnings.append(f"skills: unsupported category '{category}' in bundle; skipped")
                continue
            target_root = settings.managed_skill_directory(category)
            target_root.mkdir(parents=True, exist_ok=True)
            for skill_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
                target = target_root / skill_dir.name
                if target.exists():
                    if on_conflict == "skip":
                        report.skills_skipped += 1
                        report.warnings.append(f"skills: '{category}/{skill_dir.name}' already exists; skipped")
                        continue
                    backup = target_root / f"{skill_dir.name}.bak-{timestamp}"
                    target.rename(backup)
                    report.warnings.append(
                        f"skills: existing '{category}/{skill_dir.name}' backed up to '{backup.name}'"
                    )
                    report.skills_replaced += 1
                else:
                    report.skills_installed += 1
                shutil.copytree(skill_dir, target)
