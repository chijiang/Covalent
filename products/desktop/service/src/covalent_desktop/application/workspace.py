"""Desktop Agent and conversation use cases, independent of the HTTP transport."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Protocol

from covalent_contracts.agent import AgentSpec
from covalent_contracts.mcp import McpServerConfig
from covalent_contracts.messages import Capability
from covalent_runtime.domain.types import (
    GenerationRequest,
    GenerationResponse,
    Message,
    RunContext,
)
from covalent_runtime.engine.react import ReactAgentRuntime
from covalent_runtime.ports.registry import RuntimeRegistry
from pydantic import ValidationError

from covalent_desktop.application.agent_config import (
    DESKTOP_CAPABILITIES,
    DesktopAgentConfig,
)
from covalent_desktop.application.mcp_config import DesktopMcpService
from covalent_desktop.application.provider_config import DesktopProviderConfig
from covalent_desktop.application.titles import (
    TITLE_SYSTEM_PROMPT,
    fallback_title,
    normalize_generated_title,
)
from covalent_desktop.application.trace import (
    REASONING_DELTA_EVENTS,
    activity_item,
    is_trace_event,
    reasoning_source_marker,
    strip_activity_payload,
)

LOGGER = logging.getLogger(__name__)

# Title generation is a single cheap call, mirroring Enterprise's limits.
TITLE_MAX_TOKENS = 24
MAX_TITLE_CHARS = 255

# Reasoning deltas arrive per streaming chunk; the UI polls, so batching the
# writes to SQLite keeps a long run from thrashing the session row.
REASONING_FLUSH_INTERVAL_SECONDS = 0.25


class WorkspaceStore(Protocol):
    def list_providers(self) -> list[dict[str, object]]: ...
    def get_provider(self, name: str) -> dict[str, object] | None: ...
    def save_provider(self, definition: dict[str, object]) -> None: ...
    def delete_provider(self, name: str) -> None: ...
    def list_mcp_services(self) -> list[dict[str, object]]: ...
    def get_mcp_service(self, name: str) -> dict[str, object] | None: ...
    def save_mcp_service(self, definition: dict[str, object]) -> None: ...
    def delete_mcp_service(self, name: str) -> None: ...
    def skill_states(self) -> dict[str, bool]: ...
    def set_skill_enabled(self, name: str, enabled: bool) -> None: ...
    def list_agents(self) -> list[dict[str, object]]: ...
    def get_agent(self, name: str) -> dict[str, object] | None: ...
    def save_agent(self, definition: dict[str, object]) -> None: ...
    def list_sessions(self) -> list[dict[str, str]]: ...
    def create_session(self, agent_name: str, title: str) -> str: ...
    def delete_session(self, session_id: str) -> None: ...
    def get_session(self, session_id: str) -> dict[str, object] | None: ...
    def record_turn(self, session_id: str, user_ordinal: int | None) -> int: ...
    def truncate_from_user_message(self, session_id: str, user_index: int) -> int: ...
    def append_activity(
        self, session_id: str, items: list[dict[str, object]]
    ) -> None: ...
    def activity_detail(
        self, session_id: str, activity_id: str
    ) -> dict[str, object] | None: ...
    def set_pending_input(
        self, session_id: str, request: dict[str, object] | None
    ) -> None: ...
    def set_suggestions(self, session_id: str, suggestions: list[str]) -> None: ...
    def set_live_reasoning(self, session_id: str, reasoning: str | None) -> None: ...
    def set_title(self, session_id: str, title: str) -> None: ...
    async def load_messages(self, scope_id: str) -> list[Message]: ...
    async def save_messages(self, scope_id: str, messages: list[Message]) -> None: ...


class ProviderCatalog(Protocol):
    def list_models(
        self, provider: DesktopProviderConfig, api_key: str
    ) -> list[str]: ...


class SkillManager(Protocol):
    def create_authored(self, name: str, content: str) -> str: ...
    def install_zip(self, name: str, encoded: str) -> str: ...
    def delete(self, name: str) -> None: ...
    async def sync_git(
        self, name: str, url: str, ref: str | None = None, subdir: str | None = None
    ) -> list[str]: ...


class McpInspector(Protocol):
    async def list_tools(self, server: McpServerConfig) -> list[dict[str, object]]: ...


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
        available_local_tools: Callable[[], list[str]] | None = None,
        skill_manager: SkillManager | None = None,
        mcp_inspector: McpInspector | None = None,
    ) -> None:
        self.store = store
        self.registry_factory = registry_factory
        self.available_skills = available_skills or (lambda: [])
        self.provider_catalog = provider_catalog
        self.available_local_tools = available_local_tools or (
            lambda: ["get_current_time"]
        )
        self.skill_manager = skill_manager
        self.mcp_inspector = mcp_inspector
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
            "local_tools": self.available_local_tools(),
            "capabilities": [
                capability.value
                for capability in Capability
                if capability in DESKTOP_CAPABILITIES
            ],
        }

    def list_mcp_services(self) -> list[dict[str, object]]:
        return self.store.list_mcp_services()

    def save_mcp_service(self, value: dict[str, object]) -> dict[str, object]:
        try:
            service = DesktopMcpService.model_validate(value)
        except ValidationError as error:
            raise WorkspaceError("invalid_mcp", error.errors()[0]["msg"]) from error
        definition = service.model_dump(mode="json")
        self.store.save_mcp_service(definition)
        return definition

    def delete_mcp_service(self, name: str) -> None:
        if self.store.get_mcp_service(name) is None:
            raise WorkspaceError("mcp_not_found", "MCP service not found", 404)
        if any(
            name in self._parse_agent(raw).mcp_servers
            for raw in self.store.list_agents()
        ):
            raise WorkspaceError("mcp_in_use", "MCP service is used by an Agent", 409)
        self.store.delete_mcp_service(name)

    def list_skills(self) -> list[dict[str, object]]:
        registry = self.registry_factory()
        states = self.store.skill_states()
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "enabled": states.get(spec.name, True),
                "executable": spec.is_executable,
                "source_type": "git"
                if spec.source_dir and "github_synced" in Path(spec.source_dir).parts
                else spec.source_type,
                "source_category": next(
                    (
                        part
                        for part in Path(spec.source_dir or "").parts
                        if part in {"built_in", "uploaded", "authored", "github_synced"}
                    ),
                    "external",
                ),
                "instructions": spec.instructions or "",
            }
            for spec in registry.manifest_skills.values()
        ]

    def set_skill_enabled(self, name: str, enabled: bool) -> dict[str, object]:
        if name not in {item["name"] for item in self.list_skills()}:
            raise WorkspaceError("skill_not_found", "Skill not found", 404)
        self.store.set_skill_enabled(name, enabled)
        return {"name": name, "enabled": enabled}

    def create_skill(self, name: str, content: str) -> dict[str, object]:
        if self.skill_manager is None:
            raise WorkspaceError(
                "skills_unavailable", "Skill management unavailable", 503
            )
        if name in {item["name"] for item in self.list_skills()}:
            raise WorkspaceError("skill_exists", "Skill already exists", 409)
        try:
            skill_name = self.skill_manager.create_authored(name, content)
        except (ValueError, OSError) as error:
            raise WorkspaceError("invalid_skill", str(error)) from error
        return {"name": skill_name}

    def update_skill(self, name: str, content: str) -> dict[str, object]:
        skill = next(
            (item for item in self.list_skills() if item["name"] == name), None
        )
        if not skill or skill["source_category"] != "authored":
            raise WorkspaceError(
                "skill_read_only", "Only authored skills can be edited", 409
            )
        try:
            skill_name = self.skill_manager.create_authored(name, content)
        except (ValueError, OSError) as error:
            raise WorkspaceError("invalid_skill", str(error)) from error
        return {"name": skill_name}

    def install_skill(self, name: str, archive: str) -> dict[str, object]:
        if self.skill_manager is None:
            raise WorkspaceError(
                "skills_unavailable", "Skill management unavailable", 503
            )
        try:
            skill_name = self.skill_manager.install_zip(name, archive)
        except (ValueError, OSError) as error:
            raise WorkspaceError("invalid_skill", str(error)) from error
        self.store.set_skill_enabled(skill_name, False)
        return {"name": skill_name, "enabled": False}

    def delete_skill(self, name: str) -> None:
        if any(
            name in self._parse_agent(raw).skills for raw in self.store.list_agents()
        ):
            raise WorkspaceError("skill_in_use", "Skill is used by an Agent", 409)
        if self.skill_manager is None:
            raise WorkspaceError(
                "skills_unavailable", "Skill management unavailable", 503
            )
        try:
            self.skill_manager.delete(name)
        except ValueError as error:
            raise WorkspaceError("invalid_skill", str(error)) from error

    async def sync_git_skills(
        self, name: str, url: str, ref: str | None = None, subdir: str | None = None
    ) -> dict[str, object]:
        if self.skill_manager is None:
            raise WorkspaceError(
                "skills_unavailable", "Skill management unavailable", 503
            )
        try:
            names = await self.skill_manager.sync_git(name, url, ref, subdir)
        except (ValueError, OSError) as error:
            raise WorkspaceError("invalid_skill_source", str(error)) from error
        existing_states = self.store.skill_states()
        for skill_name in names:
            if skill_name not in existing_states:
                self.store.set_skill_enabled(skill_name, False)
        return {"items": names}

    async def inspect_mcp_service(
        self, name: str, env: dict[str, str] | None = None
    ) -> list[dict[str, object]]:
        raw = self.store.get_mcp_service(name)
        if not raw:
            raise WorkspaceError("mcp_not_found", "MCP service not found", 404)
        service = DesktopMcpService.model_validate(raw)
        if self.mcp_inspector is None:
            raise WorkspaceError("mcp_unavailable", "MCP inspector unavailable", 503)
        return await self.mcp_inspector.list_tools(service.to_contract(env))

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
        unsupported_tools = set(config.local_tools) - set(self.available_local_tools())
        if unsupported_tools:
            raise WorkspaceError(
                "unknown_tool",
                f"Local tool is not available: {', '.join(sorted(unsupported_tools))}",
            )
        missing_mcp = {
            name
            for name in config.mcp_servers
            if not (raw := self.store.get_mcp_service(name))
            or not raw.get("enabled", True)
        }
        if missing_mcp:
            raise WorkspaceError(
                "missing_mcp",
                f"MCP service is not available: {', '.join(sorted(missing_mcp))}",
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
        session["activity"] = self._strip_activity(session.get("activity"))
        return session

    def get_session_activity(
        self, session_id: str, activity_id: str
    ) -> dict[str, object]:
        item = self.store.activity_detail(session_id, activity_id)
        if item is None:
            raise WorkspaceError("activity_not_found", "Trace entry not found", 404)
        return item

    def _strip_activity(self, activity: object) -> list[dict[str, object]]:
        if not isinstance(activity, list):
            return []
        stripped: list[dict[str, object]] = []
        for item in activity:
            payload, flags = strip_activity_payload(item.get("payload"))
            stripped.append({**item, "payload": payload, **flags})
        return stripped

    def create_chat_session(self, agent_name: str, title: str) -> dict[str, object]:
        raw_definition = self.store.get_agent(agent_name)
        if raw_definition is None:
            raise WorkspaceError("agent_not_found", "Agent not found", 404)
        config = self._parse_agent(raw_definition)
        if not config.enabled or Capability.CHAT not in config.capabilities:
            raise WorkspaceError(
                "agent_disabled", "Agent is not available for chat", 409
            )
        return {
            "session_id": self.store.create_session(agent_name, fallback_title(title))
        }

    def rename_session(self, session_id: str, title: str) -> dict[str, object]:
        cleaned = " ".join(title.split())
        if not cleaned or len(cleaned) > MAX_TITLE_CHARS:
            raise WorkspaceError("invalid_title", "Title must contain 1–255 characters")
        if self.store.get_session(session_id) is None:
            raise WorkspaceError("session_not_found", "Conversation not found", 404)
        self.store.set_title(session_id, cleaned)
        return {"id": session_id, "title": cleaned}

    async def send_message(
        self,
        agent_name: str,
        message: str,
        session_id: str | None = None,
        *,
        provider_keys: dict[str, str] | None = None,
        mcp_env: dict[str, dict[str, str]] | None = None,
        resume_answers: dict[str, object] | None = None,
        edit_user_index: int | None = None,
    ) -> dict[str, object]:
        raw_definition = self.store.get_agent(agent_name)
        if raw_definition is None:
            raise WorkspaceError("agent_not_found", "Agent not found", 404)
        config = self._parse_agent(raw_definition)
        if not config.enabled or Capability.CHAT not in config.capabilities:
            raise WorkspaceError(
                "agent_disabled", "Agent is not available for chat", 409
            )
        if edit_user_index is not None:
            if resume_answers is not None:
                raise WorkspaceError(
                    "invalid_edit",
                    "An edited message cannot answer a pending Agent question",
                )
            if edit_user_index < 1 or session_id is None:
                raise WorkspaceError(
                    "invalid_edit", "Editing needs a conversation and a message index"
                )
        message = message.strip()
        if (not message and resume_answers is None) or len(message) > 100000:
            raise WorkspaceError(
                "invalid_message", "Message must contain 1–100000 characters"
            )
        provider_keys = provider_keys or {}
        mcp_env = mcp_env or {}
        with self._run_lock:
            created = session_id is None
            if session_id:
                session = self.get_session(session_id)
                if session["agent_name"] != agent_name:
                    raise WorkspaceError(
                        "agent_mismatch", "Conversation belongs to another Agent"
                    )
                if (
                    session["input_request"]
                    and resume_answers is None
                    and edit_user_index is None
                ):
                    raise WorkspaceError(
                        "input_required",
                        "Answer the pending Agent question before sending another message",
                        409,
                    )
                if resume_answers is not None and not session["input_request"]:
                    raise WorkspaceError(
                        "no_pending_input", "No pending Agent question", 409
                    )
                if edit_user_index is not None:
                    # Rewriting history has to happen inside the run lock: the
                    # runtime writes the whole transcript back when the turn
                    # ends, so an out-of-band truncation would be overwritten.
                    try:
                        self.store.truncate_from_user_message(
                            session_id, edit_user_index
                        )
                    except LookupError as error:
                        raise WorkspaceError(
                            "edit_target_missing",
                            "That message is no longer part of the conversation",
                            409,
                        ) from error
                    self.store.set_pending_input(session_id, None)
                    self.store.set_suggestions(session_id, [])
            elif resume_answers is not None:
                raise WorkspaceError(
                    "no_pending_input", "No pending Agent question", 409
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
                    missing_skills = {
                        name
                        for name in candidate.skills
                        if name not in registry.skills
                        or not registry.is_skill_enabled(name)
                    }
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
                    server_configs: list[McpServerConfig] = []
                    for server_name in candidate.mcp_servers:
                        raw_server = self.store.get_mcp_service(server_name)
                        if not raw_server or not raw_server.get("enabled", True):
                            raise WorkspaceError(
                                "missing_mcp",
                                f"MCP service is not available: {server_name}",
                            )
                        server_configs.append(
                            DesktopMcpService.model_validate(raw_server).to_contract(
                                mcp_env.get(server_name)
                            )
                        )
                    registry.register_agent(
                        candidate.to_spec(provider, key, server_configs)
                    )
                agent = registry.agents[config.name]
                for candidate in reachable.values():
                    for server in registry.agents[candidate.name].mcp_servers:
                        registry.register_mcp_server(server)
                if session_id is None:
                    session_id = self.store.create_session(agent_name, message)
                runtime = ReactAgentRuntime(registry, session_store=self.store)
                metadata: dict[str, object] = {}
                if resume_answers is not None:
                    pending_input = self.get_session(session_id)["input_request"]
                    metadata["resume_tool_result"] = {
                        "tool_call_id": pending_input.get("tool_call_id"),
                        "tool_name": pending_input["tool_name"],
                        "request_id": pending_input["id"],
                        "answers": resume_answers,
                        "summary": "User answered the questions.",
                    }
                response: GenerationResponse | None = None
                pending: dict[str, object] | None = None
                # The turn opens here, so its timestamp and the user message it
                # answers are recorded together; trace entries below reuse the
                # same turn number.
                user_ordinal = None
                if resume_answers is None:
                    stored = self.get_session(session_id)["messages"]
                    user_ordinal = 1 + sum(
                        1 for item in stored if item.get("role") == "user"
                    )
                turn = self.store.record_turn(session_id, user_ordinal)
                # Trace entries are persisted as they arrive so a running turn is
                # observable before it finishes.
                live_reasoning = ""
                reasoning_source = ""
                reasoning_flushed_at = 0.0
                async for event in runtime.stream_events(
                    agent,
                    "" if resume_answers is not None else message,
                    RunContext(
                        agent_name=agent_name, session_id=session_id, metadata=metadata
                    ),
                ):
                    event_name = event["event"]
                    if is_trace_event(event_name):
                        self.store.append_activity(
                            session_id,
                            [activity_item(event_name, event["payload"], turn)],
                        )
                    if event_name in REASONING_DELTA_EVENTS:
                        payload = event["payload"]
                        text = payload.get("text") if isinstance(payload, dict) else ""
                        if isinstance(text, str) and text:
                            source = (
                                str(payload.get("agent_name") or "")
                                if event_name.startswith("delegate_")
                                else ""
                            )
                            if source != reasoning_source:
                                reasoning_source = source
                                live_reasoning += reasoning_source_marker(source)
                            live_reasoning += text
                            now = perf_counter()
                            if now - reasoning_flushed_at >= (
                                REASONING_FLUSH_INTERVAL_SECONDS
                            ):
                                reasoning_flushed_at = now
                                self.store.set_live_reasoning(
                                    session_id, live_reasoning
                                )
                    if event_name == "final":
                        response = GenerationResponse.model_validate(event["payload"])
                    elif event_name == "input_required":
                        pending = event["payload"]
                if response is None and pending is None:
                    raise RuntimeError("Runtime completed without a final response")
                if user_ordinal == 1:
                    # Name the conversation from its first message, like
                    # Enterprise. Later turns never rename it, so a manual
                    # rename always wins.
                    title = fallback_title(message)
                    try:
                        adapter = registry.get_model_provider(agent.provider)
                        suggestion = await adapter.generate(
                            GenerationRequest(
                                model=agent.provider.model,
                                system_prompt=TITLE_SYSTEM_PROMPT,
                                messages=[Message(role="user", content=message)],
                                temperature=0.0,
                                max_tokens=TITLE_MAX_TOKENS,
                            )
                        )
                        title = (
                            normalize_generated_title(suggestion.output_text) or title
                        )
                    except Exception:
                        LOGGER.debug(
                            "Conversation title generation failed", exc_info=True
                        )
                    self.store.set_title(session_id, title)
                self.store.set_pending_input(session_id, pending)
                self.store.set_suggestions(
                    session_id, response.suggestions if response else []
                )
            except Exception:
                if created and session_id is not None:
                    self.store.delete_session(session_id)
                raise
            finally:
                if session_id is not None:
                    self.store.set_live_reasoning(session_id, None)
                await registry.aclose()
            return {
                "session_id": session_id,
                "output_text": response.output_text if response else "",
                "messages": self.get_session(session_id)["messages"],
                "input_request": pending,
                "suggestions": response.suggestions if response else [],
            }
