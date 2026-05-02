from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_framework.core.types import Capability
from agent_framework.infra.db import (
    AgentCapabilityRow,
    AgentDelegateRow,
    AgentMcpServerRow,
    AgentMcpToolRow,
    AgentRow,
    AgentSkillRow,
    McpServerEnvVarRow,
    McpServerRow,
    SkillStateRow,
    SkillSourceRow,
)
from agent_framework.mcp.spec import McpServerConfig, McpToolReference
from agent_framework.model.base import ProviderConfig

ConfigKind = Literal["agents", "mcp", "skill_sources"]


class PersistedAgentConfig(BaseModel):
    name: str
    description: str
    system_prompt: str
    reasoning_prompt: str = ""
    provider: ProviderConfig
    skills: list[str] = Field(default_factory=list)
    local_tools: list[str] = Field(default_factory=list)
    delegate_agents: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    mcp_tools: list[McpToolReference] = Field(default_factory=list)
    capabilities: set[Capability] = Field(default_factory=lambda: {Capability.CHAT, Capability.REACT})
    max_iterations: int = 6
    metadata: dict[str, object] = Field(default_factory=dict)


class PersistedSkillSourceConfig(BaseModel):
    source_type: Literal["git"] = "git"
    category: Literal["github_synced"] = "github_synced"
    name: str | None = None
    url: str
    ref: str | None = None
    subdir: str | None = None


class ConfigStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_document(self, kind: ConfigKind) -> list[dict[str, object]]:
        if kind == "mcp":
            return await self._get_mcp_servers()
        if kind == "skill_sources":
            return await self._get_skill_sources()
        return await self._get_agents()

    async def save_document(self, kind: ConfigKind, payload: list[dict[str, object]]) -> list[dict[str, object]]:
        if kind == "mcp":
            await self._save_mcp_servers(payload)
        elif kind == "skill_sources":
            await self._save_skill_sources(payload)
        else:
            await self._save_agents(payload)
        return await self.get_document(kind)

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

    async def _get_skill_sources(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(SkillSourceRow).order_by(SkillSourceRow.position, SkillSourceRow.id)))

        return [
            PersistedSkillSourceConfig(
                source_type=row.source_type,
                category=row.category,
                name=row.name,
                url=row.url,
                ref=row.ref,
                subdir=row.subdir,
            ).model_dump(mode="json")
            for row in rows
        ]

    async def _save_skill_sources(self, payload: list[dict[str, object]]) -> None:
        sources = [PersistedSkillSourceConfig.model_validate(item) for item in payload]
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(delete(SkillSourceRow))
                for position, source in enumerate(sources):
                    session.add(
                        SkillSourceRow(
                            position=position,
                            source_type=source.source_type,
                            category=source.category,
                            name=source.name,
                            url=source.url,
                            ref=source.ref,
                            subdir=source.subdir,
                        )
                    )

    async def _get_mcp_servers(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            server_rows = list(await session.scalars(select(McpServerRow).order_by(McpServerRow.position, McpServerRow.name)))
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
            payload.append(
                McpServerConfig(
                    name=row.name,
                    transport=row.transport,
                    command=row.command,
                    args=row.args or [],
                    url=row.url,
                    env=env_map.get(row.name, {}),
                ).model_dump(mode="json")
            )
        return payload

    async def _save_mcp_servers(self, payload: list[dict[str, object]]) -> None:
        servers = [McpServerConfig.model_validate(item) for item in payload]
        async with self._session_factory() as session:
            async with session.begin():
                existing_rows = {row.name: row for row in list(await session.scalars(select(McpServerRow)))}
                incoming_names = {server.name for server in servers}

                for name, row in existing_rows.items():
                    if name not in incoming_names:
                        await session.delete(row)

                for position, server in enumerate(servers):
                    row = existing_rows.get(server.name)
                    if row is None:
                        row = McpServerRow(name=server.name)
                        session.add(row)
                    row.position = position
                    row.transport = server.transport
                    row.command = server.command
                    row.args = list(server.args)
                    row.url = server.url

                    await session.execute(delete(McpServerEnvVarRow).where(McpServerEnvVarRow.server_name == server.name))
                    for key, value in sorted((server.env or {}).items()):
                        session.add(McpServerEnvVarRow(server_name=server.name, key=key, value=value))

    async def _get_agents(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            agent_rows = list(await session.scalars(select(AgentRow).order_by(AgentRow.position, AgentRow.name)))
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

        payload: list[dict[str, object]] = []
        for row in agent_rows:
            payload.append(
                PersistedAgentConfig(
                    name=row.name,
                    description=row.description,
                    system_prompt=row.system_prompt,
                    reasoning_prompt=row.reasoning_prompt,
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
                    delegate_agents=delegate_map.get(row.name, []),
                    mcp_servers=mcp_map.get(row.name, []),
                    mcp_tools=mcp_tool_map.get(row.name, []),
                    capabilities={Capability(value) for value in capability_map.get(row.name, [])},
                    max_iterations=row.max_iterations,
                    metadata=row.metadata_json or {},
                ).model_dump(mode="json")
            )
        return payload

    async def _save_agents(self, payload: list[dict[str, object]]) -> None:
        agents = [PersistedAgentConfig.model_validate(item) for item in payload]
        agent_names = {agent.name for agent in agents}

        async with self._session_factory() as session:
            async with session.begin():
                known_mcp_servers = set(await session.scalars(select(McpServerRow.name)))
                for agent in agents:
                    missing_delegates = [name for name in agent.delegate_agents if name not in agent_names]
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

                await session.execute(delete(AgentCapabilityRow))
                await session.execute(delete(AgentSkillRow))
                await session.execute(delete(AgentDelegateRow))
                await session.execute(delete(AgentMcpServerRow))
                await session.execute(delete(AgentMcpToolRow))
                await session.execute(delete(AgentRow))

                for position, agent in enumerate(agents):
                    session.add(
                        AgentRow(
                            name=agent.name,
                            position=position,
                            description=agent.description,
                            system_prompt=agent.system_prompt,
                            reasoning_prompt=agent.reasoning_prompt,
                            local_tools=list(agent.local_tools),
                            provider_name=agent.provider.provider,
                            provider_model=agent.provider.model,
                            provider_api_key=agent.provider.api_key,
                            provider_base_url=agent.provider.base_url,
                            provider_timeout_seconds=agent.provider.timeout_seconds,
                            provider_extra=agent.provider.extra,
                            max_iterations=agent.max_iterations,
                            metadata_json=dict(agent.metadata),
                        )
                    )

                await session.flush()

                for agent in agents:
                    for position, capability in enumerate(agent.capabilities):
                        session.add(AgentCapabilityRow(agent_name=agent.name, capability=capability.value, position=position))
                    for position, skill_name in enumerate(agent.skills):
                        session.add(AgentSkillRow(agent_name=agent.name, skill_name=skill_name, position=position))
                    for position, delegate_name in enumerate(agent.delegate_agents):
                        session.add(
                            AgentDelegateRow(
                                agent_name=agent.name,
                                delegate_agent_name=delegate_name,
                                position=position,
                            )
                        )
                    for position, server_name in enumerate(agent.mcp_servers):
                        session.add(AgentMcpServerRow(agent_name=agent.name, server_name=server_name, position=position))
                    for position, tool_ref in enumerate(agent.mcp_tools):
                        session.add(
                            AgentMcpToolRow(
                                agent_name=agent.name,
                                server_name=tool_ref.server_name,
                                tool_name=tool_ref.tool_name,
                                position=position,
                            )
                        )