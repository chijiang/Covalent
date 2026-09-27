from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError
from covalent_desktop.infra.agent_registry import DesktopRegistryFactory
from covalent_desktop.infra.built_in_skills import (
    ENV_SOURCE,
    bundled_built_in_skills_root,
    sync_bundled_skills,
    sync_built_in_skills,
)
from covalent_desktop.infra.local_store import LocalStore
from covalent_desktop.infra.skill_manager import DesktopSkillManager
from covalent_desktop.infra.agent_registry import _ask_user
from covalent_runtime.domain.types import GenerationResponse, ToolCall


def test_embedded_mcp_migrates_to_shared_service(tmp_path: Path) -> None:
    path = tmp_path / "desktop.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        db.execute("INSERT INTO schema_version VALUES (2)")
        db.execute(
            "CREATE TABLE agents (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE providers (name TEXT PRIMARY KEY, definition TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO agents VALUES (?, ?)",
            (
                "helper",
                json.dumps(
                    {
                        "name": "helper",
                        "provider_name": "provider",
                        "model": "model",
                        "mcp_servers": [
                            {
                                "name": "files",
                                "transport": "streamable_http",
                                "url": "http://127.0.0.1:5000/mcp",
                            }
                        ],
                    }
                ),
            ),
        )
    store = LocalStore(path)
    assert store.list_agents()[0]["mcp_servers"] == ["files"]
    assert store.get_mcp_service("files")["url"] == "http://127.0.0.1:5000/mcp"
    assert LocalStore(path).list_mcp_services() == store.list_mcp_services()


def test_mcp_reference_and_runtime_credentials(tmp_path: Path) -> None:
    from test_workspace import FakeModel, FakeRegistry

    store = LocalStore(tmp_path / "desktop.sqlite3")
    model = FakeModel()
    registry = FakeRegistry(model)
    workspace = DesktopWorkspace(store, lambda: registry)
    workspace.save_provider({"name": "provider", "base_url": "https://example.com/v1"})
    workspace.save_mcp_service(
        {
            "name": "files",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "server"],
            "enabled": True,
        }
    )
    agent = workspace.save_agent(
        {
            "name": "helper",
            "provider_name": "provider",
            "model": "model",
            "mcp_servers": ["files"],
        }
    )
    assert agent["mcp_servers"] == ["files"]
    with pytest.raises(WorkspaceError, match="MCP service is used"):
        workspace.delete_mcp_service("files")
    # The MCP process is not started until the model calls it, but the exact
    # per-service environment must reach the runtime contract.
    asyncio.run(
        workspace.send_message(
            "helper",
            "hello",
            provider_keys={"provider": "key"},
            mcp_env={"files": {"TOKEN": "secret"}},
        )
    )
    assert registry.agents["helper"].mcp_servers[0].env == {"TOKEN": "secret"}


def test_managed_skills_and_local_tool_catalog(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "desktop.sqlite3")
    registry_factory = DesktopRegistryFactory(tmp_path, store)
    manager = DesktopSkillManager(registry_factory.skill_loader, tmp_path / "skills")
    workspace = DesktopWorkspace(
        store,
        registry_factory,
        registry_factory.available_skills,
        available_local_tools=registry_factory.available_local_tools,
        skill_manager=manager,
    )
    workspace.create_skill(
        "guide", "---\nname: guide\ndescription: Helpful guide\n---\nUse concise steps."
    )
    assert "guide" in workspace.agent_options()["skills"]
    assert "read_workspace_file" in workspace.agent_options()["local_tools"]
    assert "read_pdf" in workspace.agent_options()["local_tools"]
    assert "browser_navigate" in workspace.agent_options()["local_tools"]
    assert "ask_user" in workspace.agent_options()["local_tools"]
    workspace.set_skill_enabled("guide", False)
    assert "guide" not in workspace.agent_options()["skills"]
    assert workspace.list_skills()[0]["enabled"] is False


def test_ask_user_pauses_and_resumes_same_session(tmp_path: Path) -> None:
    from test_workspace import FakeModel, FakeRegistry

    class AskingModel(FakeModel):
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return GenerationResponse(
                    output_text="",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="ask_user",
                            arguments={
                                "questions": [
                                    {
                                        "header": "Choice",
                                        "question": "Which path?",
                                        "options": [{"label": "A"}, {"label": "B"}],
                                    }
                                ]
                            },
                        )
                    ],
                )
            return GenerationResponse(output_text="Continuing with A")

    model = AskingModel()
    store = LocalStore(tmp_path / "desktop.sqlite3")

    def make_registry():
        registry = FakeRegistry(model)
        registry.register_local_tool(
            "ask_user",
            {
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "description": "Ask user",
                    "parameters": {
                        "type": "object",
                        "properties": {"questions": {"type": "array"}},
                    },
                },
            },
            _ask_user,
        )
        return registry

    workspace = DesktopWorkspace(
        store, make_registry, available_local_tools=lambda: ["ask_user"]
    )
    workspace.save_provider({"name": "provider", "base_url": "https://example.com/v1"})
    workspace.save_agent(
        {
            "name": "helper",
            "provider_name": "provider",
            "model": "model",
            "local_tools": ["ask_user"],
        }
    )
    first = asyncio.run(
        workspace.send_message("helper", "Help", provider_keys={"provider": "key"})
    )
    assert first["input_request"]["questions"][0]["header"] == "Choice"
    assert workspace.get_session(first["session_id"])["input_request"] is not None
    second = asyncio.run(
        workspace.send_message(
            "helper",
            "",
            first["session_id"],
            provider_keys={"provider": "key"},
            resume_answers={"Choice": "A"},
        )
    )
    assert second["output_text"] == "Continuing with A"
    assert workspace.get_session(first["session_id"])["input_request"] is None


def test_uploaded_skill_starts_disabled_and_rejects_path_traversal(
    tmp_path: Path,
) -> None:
    store = LocalStore(tmp_path / "desktop.sqlite3")
    registry_factory = DesktopRegistryFactory(tmp_path, store)
    manager = DesktopSkillManager(registry_factory.skill_loader, tmp_path / "skills")
    workspace = DesktopWorkspace(
        store,
        registry_factory,
        registry_factory.available_skills,
        skill_manager=manager,
    )

    def archive(entries: dict[str, str]) -> str:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            for name, content in entries.items():
                output.writestr(name, content)
        return base64.b64encode(buffer.getvalue()).decode()

    result = workspace.install_skill(
        "upload-one",
        archive(
            {
                "bundle/SKILL.md": "---\nname: packaged\ndescription: Packaged skill\n---\nFollow this guidance."
            }
        ),
    )
    assert result == {"name": "packaged", "enabled": False}
    assert "packaged" not in workspace.agent_options()["skills"]
    workspace.set_skill_enabled("packaged", True)
    assert "packaged" in workspace.agent_options()["skills"]
    workspace.delete_skill("packaged")
    assert workspace.list_skills() == []
    with pytest.raises(WorkspaceError, match="unsafe path"):
        workspace.install_skill("bad", archive({"../outside/SKILL.md": "unsafe"}))
    assert not (tmp_path / "outside").exists()


def _write_skill(root: Path, name: str, description: str = "Bundled skill") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nGuidance.",
        encoding="utf-8",
    )


def test_built_in_skills_sync_mirrors_overwrites_and_prunes(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    _write_skill(source, "skill-creator")
    (source / "notes").mkdir()
    target = tmp_path / "data" / "skills" / "built_in"

    assert sync_built_in_skills(source, target) == ["skill-creator"]
    assert (target / "skill-creator" / "SKILL.md").is_file()
    assert not (target / "notes").exists()

    _write_skill(target, "legacy")
    (target / "skill-creator" / "SKILL.md").write_text("edited", encoding="utf-8")
    assert sync_built_in_skills(source, target) == ["skill-creator"]
    assert (target / "skill-creator" / "SKILL.md").read_text(
        encoding="utf-8"
    ).startswith("---")
    assert not (target / "legacy").exists()

    with pytest.raises(ValueError, match="must differ"):
        sync_built_in_skills(target, target)


def test_bundled_built_in_skills_sync_into_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bundle"
    _write_skill(source, "skill-creator", description="Create new skills")
    monkeypatch.setenv(ENV_SOURCE, str(source))
    data_dir = tmp_path / "data"

    assert sync_bundled_skills(data_dir / "skills" / "built_in") == ["skill-creator"]

    store = LocalStore(data_dir / "desktop.sqlite3")
    registry_factory = DesktopRegistryFactory(data_dir, store)
    workspace = DesktopWorkspace(
        store,
        registry_factory,
        registry_factory.available_skills,
        skill_manager=DesktopSkillManager(
            registry_factory.skill_loader, data_dir / "skills"
        ),
    )
    skills = {item["name"]: item for item in workspace.list_skills()}
    assert skills["skill-creator"]["source_category"] == "built_in"
    assert "skill-creator" in workspace.agent_options()["skills"]

    with pytest.raises(WorkspaceError, match="authored and uploaded"):
        workspace.delete_skill("skill-creator")

    monkeypatch.setenv(ENV_SOURCE, str(tmp_path / "missing"))
    assert bundled_built_in_skills_root() is None
    assert sync_bundled_skills(tmp_path / "elsewhere") == []
