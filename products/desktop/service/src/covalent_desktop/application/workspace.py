"""Desktop Agent and conversation use cases, independent of the HTTP transport."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol

from covalent_contracts.agent import AgentSpec
from covalent_contracts.model import ProviderConfig
from covalent_runtime.domain.types import Message, RunContext
from covalent_runtime.engine.react import ReactAgentRuntime
from covalent_runtime.ports.registry import RuntimeRegistry


class WorkspaceStore(Protocol):
    def list_agents(self) -> list[dict[str, object]]: ...
    def get_agent(self, name: str) -> dict[str, object] | None: ...
    def save_agent(self, definition: dict[str, object]) -> None: ...
    def list_sessions(self) -> list[dict[str, str]]: ...
    def create_session(self, agent_name: str, title: str) -> str: ...
    def delete_session(self, session_id: str) -> None: ...
    def get_session(self, session_id: str) -> dict[str, object] | None: ...
    async def load_messages(self, scope_id: str) -> list[Message]: ...
    async def save_messages(self, scope_id: str, messages: list[Message]) -> None: ...


class DesktopRegistry(RuntimeRegistry, Protocol):
    async def aclose(self) -> None: ...


NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")


@dataclass
class WorkspaceError(Exception):
    code: str
    message: str
    status: int = 400


class DesktopWorkspace:
    def __init__(
        self, store: WorkspaceStore, registry_factory: Callable[[], DesktopRegistry]
    ) -> None:
        self.store = store
        self.registry_factory = registry_factory
        # A single local run at a time keeps session history ordered without
        # coupling asyncio locks to the HTTP server's per-request event loops.
        self._run_lock = threading.Lock()

    def list_agents(self) -> list[dict[str, object]]:
        return self.store.list_agents()

    def save_agent(self, value: dict[str, object]) -> dict[str, object]:
        name = str(value.get("name", "")).strip()
        if not NAME_PATTERN.fullmatch(name):
            raise WorkspaceError(
                "invalid_name",
                "Agent name must start with a letter and contain only letters, numbers, _ or -",
            )
        model = str(value.get("model", "")).strip()
        base_url = str(value.get("base_url", "")).strip()
        if not model or not base_url.startswith(
            ("https://", "http://127.0.0.1:", "http://localhost:")
        ):
            raise WorkspaceError(
                "invalid_provider",
                "Provide a model and HTTPS endpoint (or local loopback endpoint)",
            )
        definition: dict[str, object] = {
            "name": name,
            "description": str(value.get("description", "")).strip()[:500],
            "system_prompt": str(value.get("system_prompt", "")).strip()[:20000]
            or "You are a helpful assistant.",
            "model": model,
            "base_url": base_url.rstrip("/"),
        }
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
        api_key: str | None = None,
    ) -> dict[str, object]:
        definition = self.store.get_agent(agent_name)
        if definition is None:
            raise WorkspaceError("agent_not_found", "Agent not found", 404)
        message = message.strip()
        if not message or len(message) > 100000:
            raise WorkspaceError(
                "invalid_message", "Message must contain 1–100000 characters"
            )
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise WorkspaceError(
                "missing_credential",
                "Save a model API key in Provider settings before starting a chat",
            )
        with self._run_lock:
            created = session_id is None
            if session_id:
                session = self.get_session(session_id)
                if session["agent_name"] != agent_name:
                    raise WorkspaceError(
                        "agent_mismatch", "Conversation belongs to another Agent"
                    )
            else:
                session_id = self.store.create_session(agent_name, message)
            provider = ProviderConfig(
                provider="openai_compatible",
                model=str(definition["model"]),
                base_url=str(definition["base_url"]),
                api_key=api_key,
            )
            agent = AgentSpec(
                name=agent_name,
                description=str(definition["description"]),
                system_prompt=str(definition["system_prompt"]),
                provider=provider,
            )
            registry = self.registry_factory()
            runtime = ReactAgentRuntime(registry, session_store=self.store)
            try:
                response = await runtime.run(
                    agent,
                    message,
                    RunContext(agent_name=agent_name, session_id=session_id),
                )
            except Exception:
                if created:
                    self.store.delete_session(session_id)
                raise
            finally:
                await registry.aclose()
            return {
                "session_id": session_id,
                "output_text": response.output_text,
                "messages": self.get_session(session_id)["messages"],
            }
