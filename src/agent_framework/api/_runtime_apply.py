"""Runtime config application helper.

Extracted from ``app.py``. ``_apply_runtime_config`` is the bridge between the
config helpers and the runtime registry. It imports ``_reload_git_skills`` from
``_skill_helpers`` at module level and the three config-builders lazily inside
the function body to avoid an import cycle with ``_config_helpers``.
"""

from __future__ import annotations

from fastapi import FastAPI

from agent_framework.api._skill_helpers import _reload_git_skills
from agent_framework.infra.config_store import ConfigKind, ConfigStore
from agent_framework.infra.settings import AppSettings
from agent_framework.mcp.client import McpSdkClient
from agent_framework.registry.registry import FrameworkRegistry

async def _apply_runtime_config(app: FastAPI, kind: ConfigKind, payload: list[dict[str, object]]) -> None:
    from ._config_helpers import _resolve_default_provider, _build_agent_specs, _parse_mcp_servers
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store
    settings: AppSettings = app.state.settings
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
        registry.mcp_servers.clear()
        if settings.mcp_enabled and registry.mcp_client is None:
            registry.set_mcp_client(McpSdkClient())
        for server in _parse_mcp_servers(payload):
            registry.register_mcp_server(server)
        agent_payload = await config_store.get_document("agents")
        provider_config = await _resolve_default_provider(settings, config_store)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(agent_payload, provider_config, _parse_mcp_servers(payload), settings, mcp_payload=payload)
        }
        return

    if kind == "skill_sources":
        await _reload_git_skills(app, payload)
        return

    if kind == "agents":
        mcp_payload = await config_store.get_document("mcp")
        provider_config = await _resolve_default_provider(settings, config_store)
        registry.agents = {
            agent.name: agent
            for agent in _build_agent_specs(payload, provider_config, _parse_mcp_servers(mcp_payload), settings, mcp_payload=mcp_payload)
        }
        return
