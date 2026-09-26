"""Desktop Agent and conversation use cases, independent of the HTTP transport."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from covalent_contracts.agent import AgentSpec
from covalent_contracts.mcp import McpServerConfig
from covalent_contracts.messages import Capability
from pydantic import ValidationError
from covalent_desktop.application.agent_config import (
    DESKTOP_CAPABILITIES,
    DesktopAgentConfig,
)
from covalent_desktop.application.provider_config import DesktopProviderConfig
from covalent_runtime.domain.types import Message, RunContext
from covalent_runtime.engine.react import ReactAgentRuntime
from covalent_runtime.ports.registry import RuntimeRegistry


class WorkspaceStore(Protocol):
    def list_providers(self) -> list[dict[str, object]]: ...
    def get_provider(self, name: str) -> dict[str, object] | None: ...
    def save_provider(self, definition: dict[str, object]) -> None: ...
    def delete_provider(self, name: str) -> None: ...
    def list_agents(self) -> list[dict[str, object]]: ...
    def get_agent(self, name: str) -> dict[str, object] | None: ...
    def save_agent(self, definition: dict[str, object]) -> None: ...
    def list_sessions(self) -> list[dict[str, str]]: ...
    def create_session(self, agent_name: str, title: str) -> str: ...
    def delete_session(self, session_id: str) -> None: ...
    def get_session(self, session_id: str) -> dict[str, object] | None: ...
    async def load_messages(self, scope_id: str) -> list[Message]: ...
    async def save_messages(self, scope_id: str, messages: list[Message]) -> None: ...


class ProviderCatalog(Protocol):
    def list_models(
        self, provider: DesktopProviderConfig, api_key: str
    ) -> list[str]: ...


class DesktopRegistry(RuntimeRegistry, Protocol):
    def register_agent(self, agent: AgentSpec) -> None: ...
    def register_mcp_server(self, server: McpServerConfig) -> None: ...
    async def aclose(self) -> None: ...


@dataclass
class WorkspaceError(Exception):
    code: str
    message: str
    status: int = 400


class DesktopWorkspace:
    def __init__(
        self,
        store: WorkspaceStore,
        registry_factory: Callable[[], DesktopRegistry],
        available_skills: Callable[[], list[str]] | None = None,
        provider_catalog: ProviderCatalog | None = None,
    ) -> None:
        self.store = store
        self.registry_factory = registry_factory
        self.available_skills = available_skills or (lambda: [])
        self.provider_catalog = provider_catalog
        # A single local run at a time keeps session history ordered without
        # coupling asyncio locks to the HTTP server's per-request event loops.
        self._run_lock = threading.Lock()

    def list_agents(self) -> list[dict[str, object]]:
        return [
            self._parse_agent(item).model_dump(mode="json")
            for item in self.store.list_agents()
        ]

    def list_providers(self) -> list[dict[str, object]]:
        return self.store.list_providers()

    def save_provider(self, value: dict[str, object]) -> dict[str, object]:
        try:
            provider = DesktopProviderConfig.model_validate(value)
        except ValidationError as error:
            raise WorkspaceError(
                "invalid_provider", error.errors()[0]["msg"]
            ) from error
        existing = self.store.get_provider(provider.name)
        provider.legacy_credential = bool(
            existing and existing.get("legacy_credential")
        )
        definition = provider.model_dump(mode="json")
        self.store.save_provider(definition)
        return definition

    def delete_provider(self, name: str) -> None:
        if self.store.get_provider(name) is None:
            raise WorkspaceError("provider_not_found", "Provider not found", 404)
        if any(
            self._parse_agent(raw).provider_name == name
            for raw in self.store.list_agents()
        ):
            raise WorkspaceError("provider_in_use", "Provider is used by an Agent", 409)
        self.store.delete_provider(name)

    def load_provider_models(self, name: str, api_key: str) -> list[str]:
        raw = self.store.get_provider(name)
        if raw is None:
            raise WorkspaceError("provider_not_found", "Provider not found", 404)
        if not api_key:
            raise WorkspaceError(
                "missing_credential", "Save the Provider API key before loading models"
            )
        if self.provider_catalog is None:
            raise WorkspaceError(
                "catalog_unavailable", "Model catalog is unavailable", 503
            )
        return self.provider_catalog.list_models(
            DesktopProviderConfig.model_validate(raw), api_key
        )

    def agent_options(self) -> dict[str, object]:
        return {
            "skills": self.available_skills(),
            "local_tools": ["get_current_time"],
            "capabilities": [
                capability.value
                for capability in Capability
                if capability in DESKTOP_CAPABILITIES
            ],
        }

    def _parse_agent(self, value: dict[str, object]) -> DesktopAgentConfig:
        try:
            return DesktopAgentConfig.model_validate(value)
        except ValidationError as error:
            raise WorkspaceError("invalid_agent", error.errors()[0]["msg"]) from error

    def save_agent(self, value: dict[str, object]) -> dict[str, object]:
        config = self._parse_agent(value)
        if self.store.get_provider(config.provider_name) is None:
            raise WorkspaceError(
                "provider_not_found", "Configure this Provider before saving the Agent"
            )
        missing_skills = set(config.skills) - set(self.available_skills())
        if missing_skills:
            raise WorkspaceError(
                "missing_skill",
                f"Skill is not available: {', '.join(sorted(missing_skills))}",
            )
        unsupported_tools = set(config.local_tools) - {"get_current_time"}
        if unsupported_tools:
            raise WorkspaceError(
                "unknown_tool",
                f"Local tool is not available: {', '.join(sorted(unsupported_tools))}",
            )
        available_agents = {
            item.name
            for raw in self.store.list_agents()
            if (item := self._parse_agent(raw)).enabled
        }
        missing_delegates = set(config.delegate_agents) - available_agents
        if missing_delegates:
            raise WorkspaceError(
                "missing_delegate",
                f"Sub-agent is not available: {', '.join(sorted(missing_delegates))}",
            )
        definition = config.model_dump(mode="json")
        self.store.save_agent(definition)
        return definition

    def list_sessions(self) -> list[dict[str, str]]:
        return self.store.list_sessions()

    def get_session(self, session_id: str) -> dict[str, object]:
        session = self.store.get_session(session_id)
        if session is None:
            raise WorkspaceError("session_not_found", "Conversation not found", 404)
        return session

    async def send_message(
        self,
        agent_name: str,
        message: str,
        session_id: str | None = None,
        *,
        provider_keys: dict[str, str] | None = None,
    ) -> dict[str, object]:
        raw_definition = self.store.get_agent(agent_name)
        if raw_definition is None:
            raise WorkspaceError("agent_not_found", "Agent not found", 404)
        config = self._parse_agent(raw_definition)
        if not config.enabled or Capability.CHAT not in config.capabilities:
            raise WorkspaceError(
                "agent_disabled", "Agent is not available for chat", 409
            )
        message = message.strip()
        if not message or len(message) > 100000:
            raise WorkspaceError(
                "invalid_message", "Message must contain 1–100000 characters"
            )
        provider_keys = provider_keys or {}
        with self._run_lock:
            created = session_id is None
            if session_id:
                session = self.get_session(session_id)
                if session["agent_name"] != agent_name:
                    raise WorkspaceError(
                        "agent_mismatch", "Conversation belongs to another Agent"
                    )
            registry = self.registry_factory()
            try:
                candidates = {
                    candidate.name: candidate
                    for raw in self.store.list_agents()
                    if (candidate := self._parse_agent(raw)).enabled
                }
                reachable: dict[str, DesktopAgentConfig] = {}
                pending = [config.name]
                while pending:
                    name = pending.pop()
                    if name in reachable:
                        continue
                    candidate = candidates.get(name)
                    if candidate is None:
                        raise WorkspaceError(
                            "missing_delegate", f"Sub-agent is not available: {name}"
                        )
                    missing_skills = set(candidate.skills) - set(registry.skills)
                    if missing_skills:
                        raise WorkspaceError(
                            "missing_skill",
                            f"Skill is not available: {', '.join(sorted(missing_skills))}",
                        )
                    raw_provider = self.store.get_provider(candidate.provider_name)
                    if raw_provider is None:
                        raise WorkspaceError(
                            "provider_not_found",
                            f"Provider is not available: {candidate.provider_name}",
                        )
                    provider = DesktopProviderConfig.model_validate(raw_provider)
                    key = provider_keys.get(provider.name)
                    if not key:
                        raise WorkspaceError(
                            "missing_credential",
                            f"Save an API key for Provider {provider.name} before starting a chat",
                        )
                    reachable[name] = candidate
                    pending.extend(candidate.delegate_agents)
                    registry.register_agent(candidate.to_spec(provider, key))
                agent = registry.agents[config.name]
                servers: dict[str, McpServerConfig] = {}
                for candidate in reachable.values():
                    for server in candidate.mcp_servers:
                        existing = servers.get(server.name)
                        if existing and existing != server:
                            raise WorkspaceError(
                                "mcp_conflict",
                                f"MCP server name has conflicting definitions: {server.name}",
                            )
                        servers[server.name] = server
                for server in servers.values():
                    registry.register_mcp_server(server)
                if session_id is None:
                    session_id = self.store.create_session(agent_name, message)
                runtime = ReactAgentRuntime(registry, session_store=self.store)
                response = await runtime.run(
                    agent,
                    message,
                    RunContext(agent_name=agent_name, session_id=session_id),
                )
            except Exception:
                if created and session_id is not None:
                    self.store.delete_session(session_id)
                raise
            finally:
                await registry.aclose()
            return {
                "session_id": session_id,
                "output_text": response.output_text,
                "messages": self.get_session(session_id)["messages"],
            }
