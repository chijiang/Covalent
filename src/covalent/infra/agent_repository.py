"""Agent repository — data access for ``agents`` + relation tables.

Extracted from ``ConfigStore``. Visibility/name-mapping helpers stay in
``config_store`` (imported lazily here to avoid a module-level cycle).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covalent.core.types import Capability
from covalent.infra.db import (
    AgentCapabilityRow,
    AgentDelegateRow,
    AgentMcpServerRow,
    AgentMcpToolRow,
    AgentRow,
    AgentSkillRow,
    ChatSessionRow,
    McpServerRow,
)
from covalent.mcp.spec import McpToolReference
from covalent.model.base import ProviderConfig


class AgentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def _get_agents(self, principal=None) -> list[dict[str, object]]:
        from covalent.infra.config_store import (
            PersistedAgentConfig,
            _display_resource_name,
            _resource_metadata_from_row,
            _resource_name_map,
            _resource_scope_clause,
            _translate_names,
            _translate_tool_refs,
        )

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
                    allowed_outbound=row.allowed_outbound or [],
                    sandbox_profile_id=row.sandbox_profile_id,
                    delegate_agents=_translate_names(delegate_map.get(row.name, []), agent_public_map),
                    mcp_servers=_translate_names(mcp_map.get(row.name, []), mcp_public_map),
                    mcp_tools=_translate_tool_refs(mcp_tool_map.get(row.name, []), mcp_public_map),
                    capabilities={Capability(value) for value in capability_map.get(row.name, [])},
                    max_iterations=row.max_iterations,
                    metadata=row.metadata_json or {},
                    enabled=row.enabled,
                    **_resource_metadata_from_row(row),
                ).model_dump(mode="json")
            )
        return payload

    async def _save_agents(
        self,
        payload: list[dict[str, object]],
        principal=None,
        *,
        agent_renames: dict[str, str] | None = None,
    ) -> None:
        from covalent.infra.config_store import (
            PersistedAgentConfig,
            _apply_resource_metadata,
            _editable_items,
            _find_owned_row_by_public_name,
            _owned_resource_clause,
            _resource_internal_name_map,
            _resource_scope_clause,
            _scoped_resource_name,
        )

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
                    row.enabled = agent.enabled
                    row.description = agent.description
                    row.system_prompt = agent.system_prompt
                    row.reasoning_prompt = agent.reasoning_prompt
                    row.reasoning_level = agent.reasoning_level
                    row.local_tools = list(agent.local_tools)
                    row.allowed_outbound = list(agent.allowed_outbound)
                    row.sandbox_profile_id = agent.sandbox_profile_id
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
