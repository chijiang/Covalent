from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError
from covalent_desktop.infra.local_store import LocalStore
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
    return DesktopWorkspace(LocalStore(path), lambda: FakeRegistry(model))


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
            "base_url": "https://example.com/v1",
        }
    )
    assert agent["name"] == "helper"
    first = asyncio.run(workspace.send_message("helper", "hello", api_key="test-key"))
    second = asyncio.run(
        workspace.send_message(
            "helper", "again", first["session_id"], api_key="test-key"
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


def test_missing_key_does_not_create_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    workspace.save_agent(
        {"name": "helper", "model": "fake", "base_url": "https://example.com/v1"}
    )
    with pytest.raises(WorkspaceError) as error:
        asyncio.run(workspace.send_message("helper", "hello"))
    assert error.value.code == "missing_credential"
    assert workspace.list_sessions() == []


def test_authenticated_http_agent_roundtrip(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path / "desktop.sqlite3", FakeModel())
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        payload = json.dumps(
            {"name": "helper", "model": "fake", "base_url": "https://example.com/v1"}
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
            base + "/messages",
            data=json.dumps({"agent_name": "helper", "message": "hello"}).encode(),
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "X-Model-Key": "test-key",
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
