from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError
from covalent_desktop.infra.local_store import LocalStore
from covalent_desktop.infra.provider_catalog import DesktopProviderCatalog
from covalent_desktop.application.provider_config import DesktopProviderConfig
from covalent_runtime.domain.types import (
    Capability,
    GenerationRequest,
    GenerationResponse,
)
from covalent_runtime.ports.model import ModelAdapter, ProviderConfig


class FakeModel(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ProviderConfig(
                provider="openai_compatible",
                model="fake",
                base_url="https://example.com/v1",
            )
        )
        self.requests: list[GenerationRequest] = []

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.REACT,
            Capability.STREAMING,
            Capability.TOOL_CALLING,
        }

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        return GenerationResponse(output_text=f"reply {len(self.requests)}")


class FakeRegistry(FrameworkRegistry):
    def __init__(self, model: FakeModel) -> None:
        super().__init__()
        self.model = model

    def get_model_provider(self, config: ProviderConfig) -> ModelAdapter:
        return self.model


def make_workspace(path: Path, model: FakeModel) -> DesktopWorkspace:
    workspace = DesktopWorkspace(LocalStore(path), lambda: FakeRegistry(model))
    if not workspace.list_providers():
        workspace.save_provider(
            {
                "name": "test-provider",
                "base_url": "https://example.com/v1",
                "default_model": "fake",
                "api_style": "responses",
                "is_default": True,
            }
        )
    return workspace


def test_agent_and_conversation_persist_across_store_reopen(tmp_path: Path) -> None:
    model = FakeModel()
    path = tmp_path / "desktop.sqlite3"
    workspace = make_workspace(path, model)
    agent = workspace.save_agent(
        {
            "name": "helper",
            "description": "Local",
            "system_prompt": "Be concise.",
            "model": "fake",
            "provider_name": "test-provider",
        }
    )
    assert agent["name"] == "helper"
    first = asyncio.run(
        workspace.send_message(
            "helper", "hello", provider_keys={"test-provider": "test-key"}
        )
    )
    second = asyncio.run(
        workspace.send_message(
            "helper",
            "again",
            first["session_id"],
            provider_keys={"test-provider": "test-key"},
        )
    )
    assert first["output_text"] == "reply 1"
    assert second["output_text"] == "reply 2"
    assert len(model.requests) == 2
    assert any(message.content == "hello" for message in model.requests[1].messages)
    reopened = make_workspace(path, model)
    assert reopened.list_agents() == [agent]
    assert reopened.get_session(first["session_id"])["messages"] == second["messages"]
    assert len(reopened.list_sessions()) == 1


def test_missing_key_does_not_create_session(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    with pytest.raises(WorkspaceError) as error:
        asyncio.run(workspace.send_message("helper", "hello"))
    assert error.value.code == "missing_credential"
    assert workspace.list_sessions() == []


def test_agent_uses_selected_provider_and_its_own_key(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    second = workspace.save_provider(
        {
            "name": "second",
            "base_url": "https://other.example/v1",
            "default_model": "other-model",
            "api_style": "chat_completions",
            "legacy_credential": True,
        }
    )
    assert second["legacy_credential"] is False
    workspace.save_agent(
        {"name": "helper", "provider_name": "second", "model": "other-model"}
    )
    registry = FakeRegistry(model)
    active = DesktopWorkspace(workspace.store, lambda: registry)
    asyncio.run(
        active.send_message(
            "helper", "hello", provider_keys={"second": "second-secret"}
        )
    )
    spec = registry.agents["helper"]
    assert spec.provider.base_url == "https://other.example/v1"
    assert spec.provider.model == "other-model"
    assert spec.provider.api_style == "chat_completions"
    assert spec.provider.api_key == "second-secret"
    with pytest.raises(WorkspaceError) as error:
        workspace.delete_provider("second")
    assert error.value.code == "provider_in_use"


def test_delegate_uses_its_selected_provider_key(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_provider(
        {
            "name": "second",
            "base_url": "https://other.example/v1",
            "default_model": "other-model",
        }
    )
    workspace.save_agent(
        {"name": "delegate", "provider_name": "second", "model": "other-model"}
    )
    workspace.save_agent(
        {
            "name": "helper",
            "provider_name": "test-provider",
            "model": "fake",
            "delegate_agents": ["delegate"],
        }
    )
    registry = FakeRegistry(model)
    active = DesktopWorkspace(workspace.store, lambda: registry)
    asyncio.run(
        active.send_message(
            "helper",
            "hello",
            provider_keys={"test-provider": "root-key", "second": "delegate-key"},
        )
    )
    assert registry.agents["helper"].provider.api_key == "root-key"
    assert registry.agents["delegate"].provider.api_key == "delegate-key"


def test_provider_model_catalog_reads_configured_endpoint() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            assert self.path == "/v1/models"
            assert self.headers["Authorization"] == "Bearer provider-secret"
            body = json.dumps(
                {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-b"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = DesktopProviderConfig(
            name="local", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1"
        )
        assert DesktopProviderCatalog().list_models(provider, "provider-secret") == [
            "model-a",
            "model-b",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_agent_configuration_roundtrip_and_runtime_mapping(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "delegate", "model": "fake", "provider_name": "test-provider"}
    )
    configured = workspace.save_agent(
        {
            "name": "helper",
            "model": "fake",
            "provider_name": "test-provider",
            "reasoning_prompt": "Think in steps.",
            "reasoning_level": "high",
            "explicit_thinking": False,
            "timeout_seconds": 120,
            "max_iterations": 3,
            "context_window": 8192,
            "local_tools": [],
            "delegate_agents": ["delegate"],
            "capabilities": ["chat", "react", "tool_calling"],
        }
    )
    assert configured["reasoning_level"] == "high"
    assert configured["delegate_agents"] == ["delegate"]
    assert (
        make_workspace(tmp_path / "desktop.sqlite3", model).list_agents()[1]
        == configured
    )
    registry = FakeRegistry(model)
    active = DesktopWorkspace(workspace.store, lambda: registry)
    asyncio.run(
        active.send_message(
            "helper", "hello", provider_keys={"test-provider": "test-key"}
        )
    )
    spec = registry.agents["helper"]
    assert spec.reasoning_prompt == "Think in steps."
    assert spec.provider.api_style == "responses"
    assert spec.provider.timeout_seconds == 120
    assert spec.max_iterations == 3
    assert spec.context_window == 8192
    assert spec.metadata["explicit_thinking"] is False
    assert "delegate" in registry.agents


def test_disabled_agent_and_unavailable_dependencies_fail_before_session(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {
            "name": "helper",
            "model": "fake",
            "provider_name": "test-provider",
            "enabled": False,
        }
    )
    with pytest.raises(WorkspaceError, match="not available for chat"):
        asyncio.run(
            workspace.send_message(
                "helper", "hello", provider_keys={"test-provider": "test-key"}
            )
        )
    assert workspace.list_sessions() == []
    with pytest.raises(WorkspaceError) as error:
        workspace.save_agent(
            {
                "name": "other",
                "model": "fake",
                "provider_name": "test-provider",
                "skills": ["missing"],
            }
        )
    assert error.value.code == "missing_skill"


def test_mcp_validation_rejects_unsupported_or_insecure_config(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    base = {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    for server in (
        {"name": "remote", "transport": "stdio", "command": "sh"},
        {"name": "remote", "transport": "sse", "url": "http://example.com/mcp"},
        {
            "name": "remote",
            "transport": "sse",
            "url": "https://example.com/mcp",
            "env": {"TOKEN": "secret"},
        },
    ):
        with pytest.raises(WorkspaceError) as error:
            workspace.save_agent({**base, "mcp_servers": [server]})
        assert error.value.code == "invalid_agent"
    assert workspace.list_agents() == []


def test_legacy_agent_definition_gets_new_defaults(tmp_path: Path) -> None:
    path = tmp_path / "desktop.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (1)")
        db.execute(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO agents VALUES (?, ?)",
            (
                "older",
                json.dumps(
                    {
                        "name": "older",
                        "model": "fake",
                        "base_url": "https://example.com/v1",
                    }
                ),
            ),
        )
    store = LocalStore(path)
    workspace = DesktopWorkspace(store, lambda: FakeRegistry(FakeModel()))
    agent = workspace.list_agents()[0]
    assert agent["enabled"] is True
    assert agent["max_iterations"] == 6
    assert set(agent["capabilities"]) == {"chat", "react"}
    provider = workspace.list_providers()[0]
    assert agent["provider_name"] == provider["name"]
    assert provider["base_url"] == "https://example.com/v1"
    assert provider["legacy_credential"] is True


def test_authenticated_http_agent_roundtrip(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = json.dumps(
            {"name": "helper", "model": "fake", "provider_name": "test-provider"}
        ).encode()
        request = Request(
            base + "/agents",
            data=payload,
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["name"] == "helper"
        request = Request(
            base + "/agents", headers={"Authorization": "Bearer test-token"}
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["items"][0]["name"] == "helper"
        request = Request(
            base + "/agent-options", headers={"Authorization": "Bearer test-token"}
        )
        with urlopen(request, timeout=5) as response:
            options = json.load(response)
        assert "get_current_time" in options["local_tools"]
        assert "chat" in options["capabilities"]
        request = Request(
            base + "/providers", headers={"Authorization": "Bearer test-token"}
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["items"][0]["name"] == "test-provider"
        request = Request(
            base + "/messages",
            data=json.dumps(
                {
                    "agent_name": "helper",
                    "message": "hello",
                    "provider_keys": {"test-provider": "test-key"},
                }
            ).encode(),
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert result["output_text"] == "reply 1"
        request = Request(
            base + "/sessions", headers={"Authorization": "Bearer test-token"}
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["items"][0]["id"] == result["session_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
