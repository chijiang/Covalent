from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_framework.core.types import Capability
from agent_framework.infra.db import (
    AgentCapabilityRow,
    AgentDelegateRow,
    AgentMcpServerRow,
    AgentMcpToolRow,
    AgentRow,
    AgentSkillRow,
    ChatSessionRow,
    McpServerEnvVarRow,
    McpServerRow,
    ProviderRow,
    SkillStateRow,
    SkillSourceRow,
)
from agent_framework.mcp.spec import McpServerConfig, McpToolReference
from agent_framework.model.base import ProviderConfig

ConfigKind = Literal["agents", "mcp", "skill_sources", "providers"]
ResourceVisibility = Literal["private", "public"]
PublicationStatus = Literal["draft", "pending", "approved", "rejected"]


@dataclass(frozen=True)
class ConfigPrincipal:
    user_id: str
    workspace_id: str
    role: str = "member"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _resource_scope_clause(row_type: type, principal: ConfigPrincipal | None) -> object:
    if principal is None or principal.is_admin:
        return True
    return or_(
        (row_type.visibility == "public") & (row_type.publication_status == "approved"),
        row_type.owner_user_id.is_(None),
        row_type.owner_user_id == principal.user_id,
    )


def _owned_resource_clause(row_type: type, principal: ConfigPrincipal | None) -> object:
    if principal is None or principal.is_admin:
        return True
    return row_type.owner_user_id == principal.user_id


def _is_editable_by_principal(item: object, principal: ConfigPrincipal | None) -> bool:
    if principal is None or principal.is_admin:
        return True
    owner_user_id = getattr(item, "owner_user_id", None)
    if owner_user_id == principal.user_id:
        return True
    if owner_user_id not in {None, ""}:
        return False
    visibility = str(getattr(item, "visibility", "") or "").strip()
    publication_status = str(getattr(item, "publication_status", "") or "").strip()
    return visibility != "public" and publication_status != "approved"


def _editable_items(items: list[object], principal: ConfigPrincipal | None) -> list[object]:
    if principal is None or principal.is_admin:
        return items
    return [item for item in items if _is_editable_by_principal(item, principal)]


def _resource_metadata_from_row(row: object) -> dict[str, object]:
    return {
        "owner_user_id": getattr(row, "owner_user_id", None),
        "workspace_id": getattr(row, "workspace_id", None),
        "visibility": getattr(row, "visibility", "public") or "public",
        "publication_status": getattr(row, "publication_status", "approved") or "approved",
        "publication_requested_at": getattr(row, "publication_requested_at", None),
        "publication_reviewed_at": getattr(row, "publication_reviewed_at", None),
        "publication_reviewed_by_user_id": getattr(row, "publication_reviewed_by_user_id", None),
    }


def _public_resource_name(name: str, display_name: str | None) -> str:
    return display_name or name


def _internal_resource_name(row: object) -> str:
    return str(getattr(row, "name"))


def _storage_safe_component(value: str, default: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._")
    return normalized[:96] or default


def _scoped_resource_name(display_name: str, principal: ConfigPrincipal | None) -> str:
    if principal is None or principal.is_admin:
        return display_name
    suffix = _storage_safe_component(principal.user_id, "user")
    return f"{display_name}__user_{suffix}"


def _display_resource_name(row: object) -> str:
    return _public_resource_name(str(getattr(row, "name")), getattr(row, "display_name", None))


def _find_owned_row_by_public_name(rows: list[object], public_name: str) -> object | None:
    for row in rows:
        if _display_resource_name(row) == public_name:
            return row
        if _internal_resource_name(row) == public_name:
            return row
    return None


def _resource_name_map(rows: list[object]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in rows:
        internal_name = _internal_resource_name(row)
        public_name = _display_resource_name(row)
        mapping[internal_name] = public_name
        mapping[public_name] = public_name
    return mapping


def _resource_internal_name_map(rows: list[object]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in rows:
        internal_name = _internal_resource_name(row)
        public_name = _display_resource_name(row)
        mapping[internal_name] = internal_name
        mapping[public_name] = internal_name
    return mapping


def _translate_names(values: list[str], mapping: dict[str, str]) -> list[str]:
    return [mapping.get(value, value) for value in values]


def _translate_tool_refs(tool_refs: list[McpToolReference], mapping: dict[str, str]) -> list[McpToolReference]:
    return [
        tool_ref.model_copy(update={"server_name": mapping.get(tool_ref.server_name, tool_ref.server_name)})
        for tool_ref in tool_refs
    ]


def _public_mcp_config(row: McpServerRow, env: dict[str, str]) -> McpServerConfig:
    return McpServerConfig(
        name=_public_resource_name(row.name, row.display_name),
        transport=row.transport,
        command=row.command,
        args=row.args or [],
        url=row.url,
        env=env,
    )


def _provider_public_name(row: ProviderRow) -> str:
    return _public_resource_name(row.name, row.display_name)


def _normalize_resource_visibility(value: object, *, default: ResourceVisibility) -> ResourceVisibility:
    return "public" if str(value or "").strip() == "public" else default


def _normalize_publication_status(value: object, *, visibility: ResourceVisibility) -> PublicationStatus:
    normalized = str(value or "").strip()
    if normalized in {"draft", "pending", "approved", "rejected"}:
        return normalized  # type: ignore[return-value]
    return "approved" if visibility == "public" else "draft"


def _apply_resource_metadata(
    row: object,
    item: object,
    principal: ConfigPrincipal | None,
    *,
    existing_visibility: str | None = None,
    existing_status: str | None = None,
) -> None:
    visibility = _normalize_resource_visibility(
        getattr(item, "visibility", None),
        default="public" if principal is None else "private",
    )
    status = _normalize_publication_status(getattr(item, "publication_status", None), visibility=visibility)

    if principal is None:
        owner_user_id = getattr(item, "owner_user_id", None)
        workspace_id = getattr(item, "workspace_id", None)
    else:
        owner_user_id = getattr(item, "owner_user_id", None) or principal.user_id
        workspace_id = getattr(item, "workspace_id", None) or principal.workspace_id
        if not principal.is_admin:
            if visibility == "public" and existing_visibility != "public":
                visibility = "private"
                status = "pending"
            if status == "approved" and visibility != "public":
                status = "draft"

    setattr(row, "owner_user_id", owner_user_id)
    setattr(row, "workspace_id", workspace_id)
    setattr(row, "visibility", visibility)
    setattr(row, "publication_status", status)
    if status == "pending" and getattr(row, "publication_requested_at", None) is None:
        setattr(row, "publication_requested_at", datetime.now(UTC))
    if existing_status == "pending" and status in {"approved", "rejected"}:
        setattr(row, "publication_reviewed_at", datetime.now(UTC))
        setattr(row, "publication_reviewed_by_user_id", principal.user_id if principal else None)


class PersistedAgentConfig(BaseModel):
    name: str
    internal_name: str | None = None
    description: str
    system_prompt: str
    reasoning_prompt: str = ""
    reasoning_level: str = "none"
    provider: ProviderConfig
    skills: list[str] = Field(default_factory=list)
    local_tools: list[str] = Field(default_factory=list)
    delegate_agents: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    mcp_tools: list[McpToolReference] = Field(default_factory=list)
    capabilities: set[Capability] = Field(default_factory=lambda: {Capability.CHAT, Capability.REACT})
    max_iterations: int = 6
    metadata: dict[str, object] = Field(default_factory=dict)
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: ResourceVisibility = "public"
    publication_status: PublicationStatus = "approved"
    publication_requested_at: datetime | None = None
    publication_reviewed_at: datetime | None = None
    publication_reviewed_by_user_id: str | None = None


class PersistedSkillSourceConfig(BaseModel):
    source_type: Literal["git"] = "git"
    category: Literal["github_synced"] = "github_synced"
    name: str | None = None
    url: str
    ref: str | None = None
    subdir: str | None = None
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: ResourceVisibility = "public"
    publication_status: PublicationStatus = "approved"
    publication_requested_at: datetime | None = None
    publication_reviewed_at: datetime | None = None
    publication_reviewed_by_user_id: str | None = None


class PersistedProviderConfig(BaseModel):
    name: str = "default"
    internal_name: str | None = None
    provider_type: str = "openai_compatible"
    base_url: str = ""
    api_key: str | None = None
    default_model: str = ""
    is_default: bool = False
    position: int = 0
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: ResourceVisibility = "public"
    publication_status: PublicationStatus = "approved"
    publication_requested_at: datetime | None = None
    publication_reviewed_at: datetime | None = None
    publication_reviewed_by_user_id: str | None = None


class PersistedMcpServerMetadata(BaseModel):
    internal_name: str | None = None
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: ResourceVisibility = "public"
    publication_status: PublicationStatus = "approved"
    publication_requested_at: datetime | None = None
    publication_reviewed_at: datetime | None = None
    publication_reviewed_by_user_id: str | None = None


def _resolve_provider_config(existing_row: ProviderRow | None, config: PersistedProviderConfig) -> PersistedProviderConfig:
    if existing_row is None:
        return config
    return PersistedProviderConfig(
        name=config.name or existing_row.name,
        provider_type=config.provider_type,
        base_url=config.base_url,
        api_key=config.api_key if config.api_key is not None else existing_row.api_key,
        default_model=config.default_model,
        is_default=config.is_default,
        position=config.position,
        owner_user_id=config.owner_user_id if config.owner_user_id is not None else existing_row.owner_user_id,
        workspace_id=config.workspace_id if config.workspace_id is not None else existing_row.workspace_id,
        visibility=config.visibility or existing_row.visibility,
        publication_status=config.publication_status or existing_row.publication_status,
        publication_requested_at=config.publication_requested_at or existing_row.publication_requested_at,
        publication_reviewed_at=config.publication_reviewed_at or existing_row.publication_reviewed_at,
        publication_reviewed_by_user_id=config.publication_reviewed_by_user_id or existing_row.publication_reviewed_by_user_id,
    )


class ConfigStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_document(self, kind: ConfigKind, principal: ConfigPrincipal | None = None) -> list[dict[str, object]]:
        if kind == "mcp":
            return await self._get_mcp_servers(principal)
        if kind == "skill_sources":
            return await self._get_skill_sources(principal)
        if kind == "providers":
            return await self._get_providers(principal)
        return await self._get_agents(principal)

    async def save_document(
        self,
        kind: ConfigKind,
        payload: list[dict[str, object]],
        *,
        principal: ConfigPrincipal | None = None,
        agent_renames: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        if kind == "mcp":
            await self._save_mcp_servers(payload, principal)
        elif kind == "skill_sources":
            await self._save_skill_sources(payload, principal)
        elif kind == "providers":
            await self._save_providers(payload, principal)
        else:
            await self._save_agents(payload, principal, agent_renames=agent_renames)
        return await self.get_document(kind, principal)

    async def ensure_document(self, kind: ConfigKind, payload: list[dict[str, object]]) -> list[dict[str, object]]:
        existing = await self.get_document(kind)
        if existing:
            return existing
        if not payload:
            return []
        return await self.save_document(kind, payload)

    async def get_skill_state_map(self) -> dict[str, bool]:
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(SkillStateRow).order_by(SkillStateRow.skill_name)))
        return {row.skill_name: row.enabled for row in rows}

    async def set_skill_enabled(self, skill_name: str, enabled: bool) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(SkillStateRow, skill_name)
                if row is None:
                    session.add(SkillStateRow(skill_name=skill_name, enabled=enabled))
                else:
                    row.enabled = enabled

    async def delete_skill_state(self, skill_name: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(SkillStateRow, skill_name)
                if row is not None:
                    await session.delete(row)

    async def _get_skill_sources(self, principal: ConfigPrincipal | None = None) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SkillSourceRow)
                    .where(_resource_scope_clause(SkillSourceRow, principal))
                    .order_by(SkillSourceRow.position, SkillSourceRow.id)
                )
            )

        return [
            PersistedSkillSourceConfig(
                source_type=row.source_type,
                category=row.category,
                name=row.name,
                url=row.url,
                ref=row.ref,
                subdir=row.subdir,
                **_resource_metadata_from_row(row),
            ).model_dump(mode="json")
            for row in rows
        ]

    async def _save_skill_sources(self, payload: list[dict[str, object]], principal: ConfigPrincipal | None = None) -> None:
        sources = _editable_items([PersistedSkillSourceConfig.model_validate(item) for item in payload], principal)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(delete(SkillSourceRow).where(_owned_resource_clause(SkillSourceRow, principal)))
                for position, source in enumerate(sources):
                    row = SkillSourceRow(
                        position=position,
                        source_type=source.source_type,
                        category=source.category,
                        name=source.name,
                        url=source.url,
                        ref=source.ref,
                        subdir=source.subdir,
                    )
                    _apply_resource_metadata(row, source, principal)
                    session.add(row)

    async def _get_providers(self, principal: ConfigPrincipal | None = None) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(ProviderRow)
                    .where(_resource_scope_clause(ProviderRow, principal))
                    .order_by(ProviderRow.position, ProviderRow.name)
                )
            )
        payload: list[dict[str, object]] = []
        for row in rows:
            payload.append(
                PersistedProviderConfig(
                    name=_provider_public_name(row),
                    internal_name=row.name,
                    provider_type=row.provider_type,
                    base_url=row.base_url,
                    api_key=row.api_key,
                    default_model=row.default_model,
                    is_default=row.is_default,
                    position=row.position,
                    **_resource_metadata_from_row(row),
                ).model_dump(mode="json")
            )
        return payload

    async def _save_providers(self, payload: list[dict[str, object]], principal: ConfigPrincipal | None = None) -> None:
        configs = _editable_items([PersistedProviderConfig.model_validate(item) for item in payload], principal)
        async with self._session_factory() as session:
            async with session.begin():
                existing = list(await session.scalars(select(ProviderRow).where(_owned_resource_clause(ProviderRow, principal))))
                existing_map = {row.name: row for row in existing}

                for config in configs:
                    public_name = config.name
                    internal_name = config.internal_name or _scoped_resource_name(public_name, principal)
                    row = existing_map.pop(internal_name, None) or _find_owned_row_by_public_name(existing, public_name)
                    if row is not None:
                        existing_map.pop(row.name, None)
                    if row is None:
                        row = ProviderRow(name=internal_name)
                        session.add(row)
                    row.display_name = public_name if public_name != row.name else None
                    existing_visibility = row.visibility
                    existing_status = row.publication_status
                    config = _resolve_provider_config(row, config)
                    row.provider_type = config.provider_type
                    row.base_url = config.base_url
                    row.api_key = config.api_key
                    row.default_model = config.default_model
                    row.is_default = config.is_default
                    row.position = config.position
                    _apply_resource_metadata(
                        row,
                        config,
                        principal,
                        existing_visibility=existing_visibility,
                        existing_status=existing_status,
                    )

                for row in existing_map.values():
                    await session.delete(row)

    async def _get_mcp_servers(self, principal: ConfigPrincipal | None = None) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            server_rows = list(
                await session.scalars(
                    select(McpServerRow)
                    .where(_resource_scope_clause(McpServerRow, principal))
                    .order_by(McpServerRow.position, McpServerRow.name)
                )
            )
            env_rows = list(
                await session.scalars(
                    select(McpServerEnvVarRow).order_by(McpServerEnvVarRow.server_name, McpServerEnvVarRow.key)
                )
            )

        env_map: dict[str, dict[str, str]] = defaultdict(dict)
        for row in env_rows:
            env_map[row.server_name][row.key] = row.value

        payload: list[dict[str, object]] = []
        for row in server_rows:
            item = _public_mcp_config(row, env_map.get(row.name, {})).model_dump(mode="json")
            item.update(_resource_metadata_from_row(row))
            payload.append(item)
        return payload

    async def _save_mcp_servers(self, payload: list[dict[str, object]], principal: ConfigPrincipal | None = None) -> None:
        parsed_items = [
            (McpServerConfig.model_validate(item), PersistedMcpServerMetadata.model_validate(item))
            for item in payload
        ]
        parsed_items = [
            (server, metadata)
            for server, metadata in parsed_items
            if _is_editable_by_principal(metadata, principal)
        ]
        servers = [server for server, _metadata in parsed_items]
        metadata_items = [metadata for _server, metadata in parsed_items]
        async with self._session_factory() as session:
            async with session.begin():
                existing_rows = {
                    row.name: row
                    for row in list(await session.scalars(select(McpServerRow).where(_owned_resource_clause(McpServerRow, principal))))
                }
                resolved_names: list[str] = []
                for server, metadata in parsed_items:
                    resolved_names.append(metadata.internal_name or _scoped_resource_name(server.name, principal))
                incoming_names = set(resolved_names)

                for name, row in existing_rows.items():
                    if name not in incoming_names:
                        await session.delete(row)

                for position, server in enumerate(servers):
                    public_name = server.name
                    internal_name = resolved_names[position]
                    row = existing_rows.get(internal_name) or _find_owned_row_by_public_name(list(existing_rows.values()), public_name)
                    if row is None:
                        row = McpServerRow(name=internal_name)
                        session.add(row)
                    row.display_name = public_name if public_name != row.name else None
                    existing_visibility = row.visibility
                    existing_status = row.publication_status
                    row.position = position
                    row.transport = server.transport
                    row.command = server.command
                    row.args = list(server.args)
                    row.url = server.url
                    _apply_resource_metadata(
                        row,
                        metadata_items[position],
                        principal,
                        existing_visibility=existing_visibility,
                        existing_status=existing_status,
                    )

                    await session.execute(delete(McpServerEnvVarRow).where(McpServerEnvVarRow.server_name == row.name))
                    for key, value in sorted((server.env or {}).items()):
                        session.add(McpServerEnvVarRow(server_name=row.name, key=key, value=value))

    async def _get_agents(self, principal: ConfigPrincipal | None = None) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            agent_rows = list(
                await session.scalars(
                    select(AgentRow)
                    .where(_resource_scope_clause(AgentRow, principal))
                    .order_by(AgentRow.position, AgentRow.name)
                )
            )
            server_rows = list(
                await session.scalars(
                    select(McpServerRow)
                    .where(_resource_scope_clause(McpServerRow, principal))
                    .order_by(McpServerRow.position, McpServerRow.name)
                )
            )
            capability_rows = list(
                await session.scalars(select(AgentCapabilityRow).order_by(AgentCapabilityRow.agent_name, AgentCapabilityRow.position))
            )
            skill_rows = list(await session.scalars(select(AgentSkillRow).order_by(AgentSkillRow.agent_name, AgentSkillRow.position)))
            delegate_rows = list(
                await session.scalars(select(AgentDelegateRow).order_by(AgentDelegateRow.agent_name, AgentDelegateRow.position))
            )
            mcp_rows = list(
                await session.scalars(select(AgentMcpServerRow).order_by(AgentMcpServerRow.agent_name, AgentMcpServerRow.position))
            )
            mcp_tool_rows = list(
                await session.scalars(select(AgentMcpToolRow).order_by(AgentMcpToolRow.agent_name, AgentMcpToolRow.position))
            )

        capability_map: dict[str, list[str]] = defaultdict(list)
        skill_map: dict[str, list[str]] = defaultdict(list)
        delegate_map: dict[str, list[str]] = defaultdict(list)
        mcp_map: dict[str, list[str]] = defaultdict(list)
        mcp_tool_map: dict[str, list[McpToolReference]] = defaultdict(list)

        for row in capability_rows:
            capability_map[row.agent_name].append(row.capability)
        for row in skill_rows:
            skill_map[row.agent_name].append(row.skill_name)
        for row in delegate_rows:
            delegate_map[row.agent_name].append(row.delegate_agent_name)
        for row in mcp_rows:
            mcp_map[row.agent_name].append(row.server_name)
        for row in mcp_tool_rows:
            mcp_tool_map[row.agent_name].append(McpToolReference(server_name=row.server_name, tool_name=row.tool_name))

        agent_public_map = _resource_name_map(agent_rows)
        mcp_public_map = _resource_name_map(server_rows)
        payload: list[dict[str, object]] = []
        for row in agent_rows:
            payload.append(
                PersistedAgentConfig(
                    name=_display_resource_name(row),
                    internal_name=row.name,
                    description=row.description,
                    system_prompt=row.system_prompt,
                    reasoning_prompt=row.reasoning_prompt,
                    reasoning_level=row.reasoning_level,
                    provider=ProviderConfig(
                        provider=row.provider_name,
                        model=row.provider_model,
                        api_key=row.provider_api_key,
                        base_url=row.provider_base_url,
                        timeout_seconds=row.provider_timeout_seconds,
                        extra=row.provider_extra or {},
                    ),
                    skills=skill_map.get(row.name, []),
                    local_tools=row.local_tools or [],
                    delegate_agents=_translate_names(delegate_map.get(row.name, []), agent_public_map),
                    mcp_servers=_translate_names(mcp_map.get(row.name, []), mcp_public_map),
                    mcp_tools=_translate_tool_refs(mcp_tool_map.get(row.name, []), mcp_public_map),
                    capabilities={Capability(value) for value in capability_map.get(row.name, [])},
                    max_iterations=row.max_iterations,
                    metadata=row.metadata_json or {},
                    **_resource_metadata_from_row(row),
                ).model_dump(mode="json")
            )
        return payload

    async def _save_agents(
        self,
        payload: list[dict[str, object]],
        principal: ConfigPrincipal | None = None,
        *,
        agent_renames: dict[str, str] | None = None,
    ) -> None:
        agents = _editable_items([PersistedAgentConfig.model_validate(item) for item in payload], principal)
        agent_names = {agent.name for agent in agents}
        rename_map = {
            old_name: new_name
            for old_name, new_name in (agent_renames or {}).items()
            if old_name and new_name and old_name != new_name and new_name in agent_names
        }

        async with self._session_factory() as session:
            async with session.begin():
                visible_agent_rows = list(await session.scalars(select(AgentRow).where(_resource_scope_clause(AgentRow, principal))))
                visible_mcp_rows = list(await session.scalars(select(McpServerRow).where(_resource_scope_clause(McpServerRow, principal))))
                resolved_agent_names = {
                    agent.name: agent.internal_name or _scoped_resource_name(agent.name, principal)
                    for agent in agents
                }
                agent_internal_map = {**_resource_internal_name_map(visible_agent_rows), **resolved_agent_names}
                mcp_internal_map = _resource_internal_name_map(visible_mcp_rows)
                visible_agent_names = set(agent_internal_map)
                known_mcp_servers = set(mcp_internal_map)
                for agent in agents:
                    normalized_delegate_agents = [rename_map.get(name, name) for name in agent.delegate_agents]
                    missing_delegates = [name for name in normalized_delegate_agents if name not in agent_names and name not in visible_agent_names]
                    if missing_delegates:
                        raise ValueError(f"Unknown delegate agents for '{agent.name}': {', '.join(missing_delegates)}")
                    missing_mcp = [name for name in agent.mcp_servers if name not in known_mcp_servers]
                    if missing_mcp:
                        raise ValueError(f"Unknown MCP servers for '{agent.name}': {', '.join(missing_mcp)}")
                    missing_tool_servers = [tool.server_name for tool in agent.mcp_tools if tool.server_name not in known_mcp_servers]
                    if missing_tool_servers:
                        raise ValueError(
                            f"Unknown MCP servers referenced by tools for '{agent.name}': {', '.join(sorted(set(missing_tool_servers)))}"
                        )
                    unselected_servers = [tool.server_name for tool in agent.mcp_tools if tool.server_name not in agent.mcp_servers]
                    if unselected_servers:
                        raise ValueError(
                            f"MCP tools for '{agent.name}' reference unselected servers: {', '.join(sorted(set(unselected_servers)))}"
                        )

                if rename_map:
                    for old_name, new_name in rename_map.items():
                        await session.execute(
                            update(ChatSessionRow)
                            .where(ChatSessionRow.agent_name == old_name)
                            .values(agent_name=new_name, updated_at=ChatSessionRow.updated_at)
                        )

                existing_rows = list(await session.scalars(select(AgentRow).where(_owned_resource_clause(AgentRow, principal))))
                existing_map = {row.name: row for row in existing_rows}
                managed_names = set(existing_map) | set(resolved_agent_names.values())

                if managed_names:
                    await session.execute(delete(AgentCapabilityRow).where(AgentCapabilityRow.agent_name.in_(managed_names)))
                    await session.execute(delete(AgentSkillRow).where(AgentSkillRow.agent_name.in_(managed_names)))
                    await session.execute(delete(AgentDelegateRow).where(AgentDelegateRow.agent_name.in_(managed_names)))
                    await session.execute(delete(AgentMcpServerRow).where(AgentMcpServerRow.agent_name.in_(managed_names)))
                    await session.execute(delete(AgentMcpToolRow).where(AgentMcpToolRow.agent_name.in_(managed_names)))

                for position, agent in enumerate(agents):
                    public_name = agent.name
                    internal_name = resolved_agent_names[public_name]
                    row = existing_map.pop(internal_name, None) or _find_owned_row_by_public_name(existing_rows, public_name)
                    if row is not None:
                        existing_map.pop(row.name, None)
                    if row is None:
                        row = AgentRow(name=internal_name)
                        session.add(row)
                    row.display_name = public_name if public_name != row.name else None
                    existing_visibility = row.visibility
                    existing_status = row.publication_status
                    row.position = position
                    row.description = agent.description
                    row.system_prompt = agent.system_prompt
                    row.reasoning_prompt = agent.reasoning_prompt
                    row.reasoning_level = agent.reasoning_level
                    row.local_tools = list(agent.local_tools)
                    row.provider_name = agent.provider.provider
                    row.provider_model = agent.provider.model
                    row.provider_api_key = agent.provider.api_key
                    row.provider_base_url = agent.provider.base_url
                    row.provider_timeout_seconds = agent.provider.timeout_seconds
                    row.provider_extra = agent.provider.extra
                    row.max_iterations = agent.max_iterations
                    row.metadata_json = dict(agent.metadata)
                    _apply_resource_metadata(
                        row,
                        agent,
                        principal,
                        existing_visibility=existing_visibility,
                        existing_status=existing_status,
                    )

                for row in existing_map.values():
                    await session.delete(row)

                await session.flush()

                for agent in agents:
                    agent_internal_name = resolved_agent_names[agent.name]
                    normalized_delegate_agents = [rename_map.get(name, name) for name in agent.delegate_agents]
                    for position, capability in enumerate(agent.capabilities):
                        session.add(AgentCapabilityRow(agent_name=agent_internal_name, capability=capability.value, position=position))
                    for position, skill_name in enumerate(agent.skills):
                        session.add(AgentSkillRow(agent_name=agent_internal_name, skill_name=skill_name, position=position))
                    for position, delegate_name in enumerate(normalized_delegate_agents):
                        session.add(
                            AgentDelegateRow(
                                agent_name=agent_internal_name,
                                delegate_agent_name=resolved_agent_names.get(delegate_name, agent_internal_map.get(delegate_name, delegate_name)),
                                position=position,
                            )
                        )
                    for position, server_name in enumerate(agent.mcp_servers):
                        session.add(
                            AgentMcpServerRow(
                                agent_name=agent_internal_name,
                                server_name=mcp_internal_map.get(server_name, server_name),
                                position=position,
                            )
                        )
                    for position, tool_ref in enumerate(agent.mcp_tools):
                        internal_server_name = mcp_internal_map.get(tool_ref.server_name, tool_ref.server_name)
                        session.add(
                            AgentMcpToolRow(
                                agent_name=agent_internal_name,
                                server_name=internal_server_name,
                                tool_name=tool_ref.tool_name,
                                position=position,
                            )
                        )
