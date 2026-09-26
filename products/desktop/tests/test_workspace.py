from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError
from covalent_desktop.infra.local_store import LocalStore
from covalent_desktop.infra.provider_catalog import DesktopProviderCatalog
from covalent_desktop.application.provider_config import DesktopProviderConfig
from covalent_desktop.application.titles import (
    fallback_title,
    normalize_generated_title,
)
from covalent_desktop.application.trace import reasoning_source_marker
from covalent_runtime.domain.types import (
    Capability,
    GenerationRequest,
    GenerationResponse,
    Message,
    ToolCall,
)
from covalent_runtime.ports.model import ModelAdapter, ProviderConfig


TITLE_PROMPT_PREFIX = "You are a conversation title generator"


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
        # Conversation titles come from a separate call so it is recorded apart
        # from the chat turns.
        self.title_requests: list[GenerationRequest] = []

    @property
    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.REACT,
            Capability.STREAMING,
            Capability.TOOL_CALLING,
        }

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        if (request.system_prompt or "").startswith(TITLE_PROMPT_PREFIX):
            self.title_requests.append(request)
            return GenerationResponse(output_text="Generated title")
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
    assert set(agent["capabilities"]) == {"chat", "react", "streaming", "tool_calling"}
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


def test_trace_activity_is_recorded_per_turn(tmp_path: Path) -> None:
    model = FakeModel()
    path = tmp_path / "desktop.sqlite3"
    workspace = make_workspace(path, model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper", "hello", provider_keys={"test-provider": "test-key"}
        )
    )
    asyncio.run(
        workspace.send_message(
            "helper",
            "again",
            first["session_id"],
            provider_keys={"test-provider": "test-key"},
        )
    )
    session = workspace.get_session(first["session_id"])
    activity = session["activity"]
    assert {item["turn"] for item in activity} == {1, 2}
    titles = {item["title"] for item in activity}
    assert {"iteration", "model_call"} <= titles
    # Deltas and transcript events stay out of the trace log.
    assert not titles & {"assistant", "final", "assistant_delta", "reasoning_delta"}
    for item in activity:
        event_name, timestamp, suffix = item["id"].rsplit("-", 2)
        assert event_name == item["title"]
        assert timestamp.isdigit() and len(suffix) == 8
        assert item["payload"]
    persisted = make_workspace(path, model).get_session(first["session_id"])
    assert [item["turn"] for item in persisted["activity"]] == [
        item["turn"] for item in activity
    ]


def test_trace_activity_strips_raw_payload_and_serves_details(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    session_id = workspace.store.create_session("helper", "hello")
    workspace.store.append_activity(
        session_id,
        [
            {
                "id": "model_call-1700000000000-abcdef12",
                "title": "model_call",
                "payload": {
                    "model": "fake",
                    "raw_request": {"messages": [{"role": "user"}]},
                    "raw_response": {"output_text": "hi"},
                },
                "turn": workspace.store.record_turn(session_id, 1),
            }
        ],
    )
    item = workspace.get_session(session_id)["activity"][0]
    assert item["payload"] == {"model": "fake"}
    assert item["has_raw_request"] is True
    assert item["has_raw_response"] is True
    detail = workspace.get_session_activity(session_id, item["id"])
    assert detail["payload"]["raw_request"] == {"messages": [{"role": "user"}]}
    assert detail["payload"]["raw_response"] == {"output_text": "hi"}
    with pytest.raises(WorkspaceError) as error:
        workspace.get_session_activity(session_id, "missing-1-abcdef12")
    assert (error.value.code, error.value.status) == ("activity_not_found", 404)


def test_trace_activity_flags_absent_raw_payloads(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    session_id = workspace.store.create_session("helper", "hello")
    workspace.store.append_activity(
        session_id,
        [
            {
                "id": "iteration-1700000000001-abcdef12",
                "title": "iteration",
                "payload": {"iteration": 1},
                "turn": 1,
            }
        ],
    )
    item = workspace.get_session(session_id)["activity"][0]
    assert item["payload"] == {"iteration": 1}
    assert item["has_raw_request"] is False
    assert item["has_raw_response"] is False


def test_create_chat_session_validates_agent(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    with pytest.raises(WorkspaceError) as error:
        workspace.create_chat_session("missing", "hello")
    assert (error.value.code, error.value.status) == ("agent_not_found", 404)
    workspace.save_agent(
        {
            "name": "helper",
            "model": "fake",
            "provider_name": "test-provider",
            "enabled": False,
        }
    )
    with pytest.raises(WorkspaceError) as error:
        workspace.create_chat_session("helper", "hello")
    assert (error.value.code, error.value.status) == ("agent_disabled", 409)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    created = workspace.create_chat_session("helper", "x" * 200)
    session = workspace.get_session(created["session_id"])
    assert session["agent_name"] == "helper"
    assert len(session["title"]) == 51
    assert session["activity"] == []


def test_activity_column_migration_from_v5_keeps_sessions(tmp_path: Path) -> None:
    path = tmp_path / "desktop.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (5)")
        db.execute(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE mcp_services (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE skill_states (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', pending_input TEXT, suggestions TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO sessions(id, agent_name, title, messages) VALUES (?, ?, ?, ?)",
            (
                "a" * 32,
                "helper",
                "kept",
                json.dumps([{"role": "user", "content": "old"}]),
            ),
        )
    store = LocalStore(path)
    session = store.get_session("a" * 32)
    assert session is not None
    assert session["messages"] == [{"role": "user", "content": "old"}]
    assert session["activity"] == []
    assert session["live_reasoning"] is None
    store.append_activity(
        "a" * 32,
        [
            {
                "id": "iteration-1700000000002-abcdef12",
                "title": "iteration",
                "payload": {"iteration": 1},
                "turn": store.record_turn("a" * 32, 1),
            }
        ],
    )
    assert store.get_session("a" * 32)["activity"][0]["turn"] == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_authenticated_http_trace_routes(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    try:
        request = Request(
            base + "/agents",
            data=json.dumps(
                {"name": "helper", "model": "fake", "provider_name": "test-provider"}
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5):
            pass
        request = Request(
            base + "/sessions",
            data=json.dumps({"agent_name": "helper", "title": "hello"}).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            session_id = json.load(response)["session_id"]
        request = Request(
            base + "/messages",
            data=json.dumps(
                {
                    "agent_name": "helper",
                    "message": "hello",
                    "session_id": session_id,
                    "provider_keys": {"test-provider": "test-key"},
                }
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5):
            pass
        request = Request(base + f"/sessions/{session_id}", headers=headers)
        with urlopen(request, timeout=5) as response:
            activity = json.load(response)["activity"]
        assert activity
        assert {item["turn"] for item in activity} == {1}
        model_call = next(item for item in activity if item["title"] == "model_call")
        assert model_call["has_raw_request"] is True
        assert model_call["has_raw_response"] is True
        assert "raw_request" not in model_call["payload"]
        request = Request(
            base + f"/sessions/{session_id}/activity/{model_call['id']}",
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            detail = json.load(response)
        assert detail["id"] == model_call["id"]
        assert "raw_request" in detail["payload"]
        for path in (
            f"/sessions/{session_id}/activity/missing-1-abcdef12",
            f"/sessions/{session_id}/nonsense/{model_call['id']}",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + path, headers=headers), timeout=5)
            assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ThinkingModel(FakeModel):
    """Replies with a <think> block plus a tool call, then probes the session."""

    def __init__(self, store: LocalStore, session_id: str) -> None:
        super().__init__()
        self.store = store
        self.session_id = session_id
        self.live_reasoning_seen: list[object] = []

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        self.live_reasoning_seen.append(
            self.store.get_session(self.session_id)["live_reasoning"]
        )
        if len(self.requests) == 1:
            tool_call = ToolCall(id="call_1", name="get_current_time")
            return GenerationResponse(
                output_text="<think>weigh the options</think>",
                tool_calls=[tool_call],
                assistant_message=Message(
                    role="assistant",
                    content="<think>weigh the options</think>",
                    tool_calls=[
                        {"id": "call_1", "name": "get_current_time", "arguments": {}}
                    ],
                ),
            )
        return GenerationResponse(
            output_text="final answer",
            assistant_message=Message(role="assistant", content="final answer"),
        )


def test_reasoning_source_marker_matches_enterprise_wire_format() -> None:
    assert reasoning_source_marker("researcher") == "\x1e#agent:researcher\x1e"
    assert reasoning_source_marker("") == "\x1e#agent:\x1e"


def test_live_reasoning_round_trip(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "desktop.sqlite3")
    session_id = store.create_session("helper", "hello")
    assert store.get_session(session_id)["live_reasoning"] is None
    store.set_live_reasoning(session_id, "thinking")
    assert store.get_session(session_id)["live_reasoning"] == "thinking"
    store.set_live_reasoning(session_id, None)
    assert store.get_session(session_id)["live_reasoning"] is None


def test_live_reasoning_is_visible_mid_run_and_cleared_afterwards(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desktop.sqlite3"
    store = LocalStore(path)
    workspace = DesktopWorkspace(store, lambda: FakeRegistry(model))
    workspace.save_provider(
        {
            "name": "test-provider",
            "base_url": "https://example.com/v1",
            "default_model": "fake",
            "api_style": "responses",
            "is_default": True,
        }
    )
    workspace.save_agent(
        {
            "name": "helper",
            "model": "fake",
            "provider_name": "test-provider",
            "local_tools": ["get_current_time"],
        }
    )
    session_id = workspace.create_chat_session("helper", "think please")["session_id"]
    model = ThinkingModel(store, session_id)
    asyncio.run(
        workspace.send_message(
            "helper",
            "think please",
            session_id,
            provider_keys={"test-provider": "test-key"},
        )
    )
    # The second model call sees the reasoning the first one had already exposed,
    # which is what the UI polls for while the turn is still running.
    assert model.live_reasoning_seen[0] is None
    assert model.live_reasoning_seen[1] == "weigh the options"
    session = workspace.get_session(session_id)
    assert session["live_reasoning"] is None
    assistants = [item for item in session["messages"] if item["role"] == "assistant"]
    assert assistants[0]["reasoning_content"] == "weigh the options"
    assert assistants[-1]["content"] == "final answer"


def test_live_reasoning_column_migration_from_v6(tmp_path: Path) -> None:
    path = tmp_path / "desktop.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (6)")
        db.execute(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE mcp_services (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE skill_states (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', pending_input TEXT, suggestions TEXT NOT NULL DEFAULT '[]', activity TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO sessions(id, agent_name, title, activity) VALUES (?, ?, ?, ?)",
            (
                "b" * 32,
                "helper",
                "kept",
                json.dumps(
                    [
                        {
                            "id": "iteration-1700000000003-abcdef12",
                            "title": "iteration",
                            "payload": {"iteration": 1},
                            "turn": 1,
                        }
                    ]
                ),
            ),
        )
    store = LocalStore(path)
    session = store.get_session("b" * 32)
    assert session is not None
    assert session["live_reasoning"] is None
    assert session["activity"][0]["title"] == "iteration"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_turn_meta_column_migration_from_v7(tmp_path: Path) -> None:
    path = tmp_path / "desktop.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (7)")
        db.execute(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE mcp_services (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE skill_states (name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', pending_input TEXT, suggestions TEXT NOT NULL DEFAULT '[]', activity TEXT NOT NULL DEFAULT '[]', live_reasoning TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO sessions(id, agent_name, title, messages) VALUES (?, ?, ?, ?)",
            (
                "c" * 32,
                "helper",
                "kept",
                json.dumps([{"role": "user", "content": "old"}]),
            ),
        )
    store = LocalStore(path)
    session = store.get_session("c" * 32)
    assert session is not None
    assert session["messages"] == [{"role": "user", "content": "old"}]
    assert session["turn_meta"] == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 9


def test_record_turn_numbers_turns_and_user_ordinals(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper", "one", provider_keys={"test-provider": "test-key"}
        )
    )
    asyncio.run(
        workspace.send_message(
            "helper",
            "two",
            first["session_id"],
            provider_keys={"test-provider": "test-key"},
        )
    )
    turn_meta = workspace.get_session(first["session_id"])["turn_meta"]
    assert [entry["turn"] for entry in turn_meta] == [1, 2]
    assert [entry["user_ordinal"] for entry in turn_meta] == [1, 2]
    assert all(entry["started_at"] > 1_600_000_000_000 for entry in turn_meta)


def test_edit_and_resend_truncates_transcript_and_trace(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    session_id = workspace.create_chat_session("helper", "one")["session_id"]
    keys = {"test-provider": "test-key"}
    for text in ("one", "two", "three"):
        asyncio.run(
            workspace.send_message("helper", text, session_id, provider_keys=keys)
        )
    workspace.store.set_suggestions(session_id, ["stale suggestion"])

    asyncio.run(
        workspace.send_message(
            "helper",
            "two, edited",
            session_id,
            provider_keys=keys,
            edit_user_index=2,
        )
    )

    session = workspace.get_session(session_id)
    contents = [item.get("content") for item in session["messages"]]
    assert contents[0] == "one"
    assert contents[1] == "reply 1"
    assert contents[2] == "two, edited"
    assert contents[3] == "reply 4"
    assert len(session["messages"]) == 4
    # The dropped turns' trace and turn metadata go with them, and the re-run
    # reuses the freed turn number.
    assert {entry["turn"] for entry in session["turn_meta"]} == {1, 2}
    assert {item["turn"] for item in session["activity"]} == {1, 2}
    assert session["input_request"] is None
    assert session["suggestions"] == []


def test_edit_requires_an_existing_user_message(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper", "hello", provider_keys={"test-provider": "test-key"}
        )
    )
    before = workspace.get_session(first["session_id"])["messages"]
    with pytest.raises(WorkspaceError) as error:
        asyncio.run(
            workspace.send_message(
                "helper",
                "hello again",
                first["session_id"],
                provider_keys={"test-provider": "test-key"},
                edit_user_index=5,
            )
        )
    assert (error.value.code, error.value.status) == ("edit_target_missing", 409)
    assert workspace.get_session(first["session_id"])["messages"] == before


def test_edit_rejects_invalid_combinations(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper", "hello", provider_keys={"test-provider": "test-key"}
        )
    )
    with pytest.raises(WorkspaceError) as error:
        asyncio.run(
            workspace.send_message(
                "helper",
                "hello",
                first["session_id"],
                provider_keys={"test-provider": "test-key"},
                edit_user_index=1,
                resume_answers={"Question": "answer"},
            )
        )
    assert error.value.code == "invalid_edit"
    with pytest.raises(WorkspaceError) as error:
        asyncio.run(
            workspace.send_message(
                "helper",
                "hello",
                provider_keys={"test-provider": "test-key"},
                edit_user_index=1,
            )
        )
    assert error.value.code == "invalid_edit"


def test_authenticated_http_edit_round_trip(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    try:
        request = Request(
            base + "/agents",
            data=json.dumps(
                {"name": "helper", "model": "fake", "provider_name": "test-provider"}
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5):
            pass
        request = Request(
            base + "/sessions",
            data=json.dumps({"agent_name": "helper", "title": "one"}).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            session_id = json.load(response)["session_id"]
        for text in ("one", "two"):
            request = Request(
                base + "/messages",
                data=json.dumps(
                    {
                        "agent_name": "helper",
                        "message": text,
                        "session_id": session_id,
                        "provider_keys": {"test-provider": "test-key"},
                    }
                ).encode(),
                headers=headers,
            )
            with urlopen(request, timeout=5):
                pass
        request = Request(
            base + "/messages",
            data=json.dumps(
                {
                    "agent_name": "helper",
                    "message": "one, edited",
                    "session_id": session_id,
                    "provider_keys": {"test-provider": "test-key"},
                    "edit_user_index": 1,
                }
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert [item["content"] for item in result["messages"]] == [
            "one, edited",
            "reply 3",
        ]
        request = Request(base + f"/sessions/{session_id}", headers=headers)
        with urlopen(request, timeout=5) as response:
            session = json.load(response)
        assert [entry["user_ordinal"] for entry in session["turn_meta"]] == [1]
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(
                    base + "/messages",
                    data=json.dumps(
                        {
                            "agent_name": "helper",
                            "message": "bad",
                            "session_id": session_id,
                            "edit_user_index": 0,
                        }
                    ).encode(),
                    headers=headers,
                ),
                timeout=5,
            )
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class NoTitleModel(FakeModel):
    """Chat works, but the conversation-title call always fails."""

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        if (request.system_prompt or "").startswith(TITLE_PROMPT_PREFIX):
            raise RuntimeError("title model unavailable")
        return await super().generate(request)


def test_title_is_generated_from_the_first_message(tmp_path: Path) -> None:
    model = FakeModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper", "please visit bing.com", provider_keys={"test-provider": "key"}
        )
    )
    assert len(model.title_requests) == 1
    request = model.title_requests[0]
    assert request.temperature == 0.0
    assert request.max_tokens == 24
    assert [message.content for message in request.messages] == [
        "please visit bing.com"
    ]
    assert workspace.get_session(first["session_id"])["title"] == "Generated title"
    assert workspace.list_sessions()[0]["title"] == "Generated title"

    asyncio.run(
        workspace.send_message(
            "helper",
            "and a second turn",
            first["session_id"],
            provider_keys={"test-provider": "key"},
        )
    )
    assert len(model.title_requests) == 1
    assert workspace.get_session(first["session_id"])["title"] == "Generated title"


def test_title_falls_back_to_the_first_message_when_generation_fails(
    tmp_path: Path,
) -> None:
    model = NoTitleModel()
    workspace = make_workspace(tmp_path / "desktop.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = asyncio.run(
        workspace.send_message(
            "helper",
            "  short question \n for you  ",
            provider_keys={"test-provider": "key"},
        )
    )
    assert workspace.get_session(first["session_id"])["title"] == (
        "short question for you"
    )

    long_message = "x" * 200
    second = asyncio.run(
        workspace.send_message(
            "helper", long_message, provider_keys={"test-provider": "key"}
        )
    )
    title = workspace.get_session(second["session_id"])["title"]
    assert len(title) == 51
    assert title.endswith("...")


def test_title_helpers_match_enterprise_normalization() -> None:
    assert fallback_title("") == "New conversation"
    assert fallback_title("  a \n b  ") == "a b"
    assert fallback_title("-:,. trimmed ,.-:") == "trimmed"
    assert fallback_title("y" * 60) == f"{'y' * 48}..."
    assert fallback_title("z" * 48) == "z" * 48

    assert normalize_generated_title('  "Quoted title"  ') == "Quoted title"
    assert normalize_generated_title("'single'") == "single"
    assert normalize_generated_title("line\none") == "line one"
    assert normalize_generated_title("   ") == ""
    assert len(normalize_generated_title("w" * 100)) == 60
    assert normalize_generated_title("v" * 70).endswith("v")


def test_rename_session_validates_and_persists(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    created = workspace.create_chat_session("helper", "original title")
    session_id = created["session_id"]

    renamed = workspace.rename_session(session_id, "  Renamed conversation ")
    assert renamed == {"id": session_id, "title": "Renamed conversation"}
    assert workspace.get_session(session_id)["title"] == "Renamed conversation"
    assert workspace.list_sessions()[0]["title"] == "Renamed conversation"

    for bad in ("", "   ", "x" * 256):
        with pytest.raises(WorkspaceError) as error:
            workspace.rename_session(session_id, bad)
        assert (error.value.code, error.value.status) == ("invalid_title", 400)
    assert workspace.get_session(session_id)["title"] == "Renamed conversation"

    with pytest.raises(WorkspaceError) as error:
        workspace.rename_session("f" * 32, "anything")
    assert (error.value.code, error.value.status) == ("session_not_found", 404)


def test_pin_moves_session_to_top_and_unpins(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    first = workspace.create_chat_session("helper", "one")["session_id"]
    second = workspace.create_chat_session("helper", "two")["session_id"]
    assert [item["id"] for item in workspace.list_sessions()] == [second, first]

    workspace.set_session_pinned(first, True)
    listing = workspace.list_sessions()
    assert listing[0]["id"] == first
    assert listing[0]["pinned"] is True
    assert [item["pinned"] for item in listing] == [True, False]

    workspace.set_session_pinned(first, False)
    listing = workspace.list_sessions()
    assert listing[0]["id"] == second
    assert listing[0]["pinned"] is False


def test_delete_session_requires_existing_session(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    created = workspace.create_chat_session("helper", "bye")["session_id"]
    with pytest.raises(WorkspaceError) as error:
        workspace.delete_session("f" * 32)
    assert (error.value.code, error.value.status) == ("session_not_found", 404)
    workspace.delete_session(created)
    with pytest.raises(WorkspaceError) as error:
        workspace.get_session(created)
    assert error.value.code == "session_not_found"
    assert workspace.list_sessions() == []


def test_authenticated_http_pin_and_delete_round_trip(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    try:
        request = Request(
            base + "/agents",
            data=json.dumps(
                {"name": "helper", "model": "fake", "provider_name": "test-provider"}
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5):
            pass
        request = Request(
            base + "/sessions",
            data=json.dumps({"agent_name": "helper", "title": "pin me"}).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            session_id = json.load(response)["session_id"]

        request = Request(
            base + f"/sessions/{session_id}",
            data=json.dumps({"pinned": True}).encode(),
            method="PATCH",
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response) == {"id": session_id, "pinned": True}
        request = Request(base + "/sessions", headers=headers)
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["items"][0]["pinned"] is True

        request = Request(
            base + f"/sessions/{session_id}", method="DELETE", headers=headers
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response) == {"deleted": True}
        request = Request(base + "/sessions", headers=headers)
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["items"] == []
        request = Request(
            base + f"/sessions/{session_id}", method="DELETE", headers=headers
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_authenticated_http_rename_round_trip(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
    }
    try:
        request = Request(
            base + "/agents",
            data=json.dumps(
                {"name": "helper", "model": "fake", "provider_name": "test-provider"}
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5):
            pass
        request = Request(
            base + "/sessions",
            data=json.dumps({"agent_name": "helper", "title": "first"}).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            session_id = json.load(response)["session_id"]
        request = Request(
            base + f"/sessions/{session_id}",
            data=json.dumps({"title": "Renamed over HTTP"}).encode(),
            method="PATCH",
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response) == {
                "id": session_id,
                "title": "Renamed over HTTP",
            }
        request = Request(
            base + f"/sessions/{session_id}",
            data=json.dumps({"title": "   "}).encode(),
            method="PATCH",
            headers=headers,
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 400
        request = Request(
            base + f"/sessions/{'9' * 32}",
            data=json.dumps({"title": "ghost"}).encode(),
            method="PATCH",
            headers=headers,
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
