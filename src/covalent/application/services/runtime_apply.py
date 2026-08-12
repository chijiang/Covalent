"""Runtime config application helper.

Applies a persisted config document to the live runtime registry. Framework-
independent: dependencies (registry, config_store, settings, loader) are passed
in explicitly rather than read from ``app.state``.
"""

from __future__ import annotations

from covalent.application.services.skill_service import _reload_git_skills
from covalent.infra.config_store import ConfigKind, ConfigStore
from covalent.infra.settings import AppSettings
from covalent.mcp.client import McpSdkClient
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.backend import ExecutionBackend
from covalent.skills.loader import SkillLoader


async def _apply_runtime_config(
    registry: FrameworkRegistry,
    config_store: ConfigStore,
    settings: AppSettings,
    loader: SkillLoader,
    execution_backend: ExecutionBackend,
    kind: ConfigKind,
    payload: list[dict[str, object]],
) -> None:
    from .management_service import _resolve_default_provider, _build_agent_specs, _parse_mcp_servers
    provider_config = await _resolve_default_provider(settings, config_store)

    if kind == "providers":
        mcp_payload = await config_store.get_document("mcp")
        agent_payload = await config_store.get_document("agents")
        provider_config = await _resolve_default_provider(settings, config_store, payload)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(agent_payload, provider_config, _parse_mcp_servers(mcp_payload), settings, mcp_payload=mcp_payload)
        }
        return

    if kind == "mcp":
        # Build the complete candidate before mutating the registry: if any build
        # step fails, the live registry is left untouched (no half-applied state).
        new_servers = {server.name: server for server in _parse_mcp_servers(payload)}
        agent_payload = await config_store.get_document("agents")
        provider_config = await _resolve_default_provider(settings, config_store)
        new_agents = {
            agent.name: agent
            for agent in _build_agent_specs(agent_payload, provider_config, _parse_mcp_servers(payload), settings, mcp_payload=payload)
        }
        if settings.mcp_enabled and registry.mcp_client is None:
            registry.set_mcp_client(McpSdkClient())
        registry.mcp_servers = new_servers
        registry.agents = new_agents
        return

    if kind == "skill_sources":
        await _reload_git_skills(registry, loader, config_store, execution_backend, payload)
        return

    if kind == "agents":
        mcp_payload = await config_store.get_document("mcp")
        provider_config = await _resolve_default_provider(settings, config_store)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(payload, provider_config, _parse_mcp_servers(mcp_payload), settings, mcp_payload=mcp_payload)
        }
        return
