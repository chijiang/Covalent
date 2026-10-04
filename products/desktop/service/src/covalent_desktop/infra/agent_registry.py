"""Desktop composition for locally available Agent tools, skills, and MCP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from covalent_agent_kit.browser_manager import BrowserManager
from covalent_agent_kit.mcp.client import McpSdkClient
from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_agent_kit.skills.loader import SkillLoader
from covalent_agent_kit.skills.meta_tools import register_skill_meta_tools
from covalent_agent_kit.skills.process import SkillProcessManager
from covalent_agent_kit.tools.browser_tools import register_browser_tools
from covalent_agent_kit.tools.pdf_tools import register_pdf_tools
from covalent_agent_kit.tools.workspace_tools import register_workspace_tools
from covalent_contracts.messages import (
    UserInputRequest,
    UserQuestion,
    UserQuestionOption,
)
from covalent_execution_native.backend import FileSystemBackend

from covalent_desktop.application.system_agents import (
    calculate_weighted_options,
    validate_task_state,
)
from covalent_desktop.infra.local_store import LocalStore


@dataclass
class DesktopSkillSettings:
    root: Path
    skills_directories: str | None = None
    session_workspace_enabled: bool = True

    def local_skill_directories(self) -> list[Path]:
        return [self.root]

    def managed_skill_directory(self, category: str) -> Path:
        return self.root / category

    def workspace_root(self) -> Path:
        path = self.root.parent / "workspaces"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def session_workspace_dir(self, session_id: str) -> Path:
        path = self.workspace_root() / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class DesktopRegistryFactory:
    def __init__(self, data_dir: Path, store: LocalStore) -> None:
        self.settings = DesktopSkillSettings(data_dir / "skills")
        self.skill_loader = SkillLoader(self.settings)
        self.store = store
        # Set by bootstrap once the service port is bound; published files are
        # then advertised as absolute loopback URLs the agent's browser can open.
        self.service_base_url: str | None = None

    def downloads_root(self) -> Path:
        return self.settings.workspace_root() / ".covalent" / "downloads"

    def available_skills(self) -> list[str]:
        states = self.store.skill_states()
        return sorted(
            spec.name
            for spec in self.skill_loader.discover_local()
            if states.get(spec.name, True)
        )

    def available_local_tools(self) -> list[str]:
        registry = self()
        return sorted(
            name
            for name in registry.local_tools
            if name
            not in {
                "list_skill_files",
                "read_skill_instructions",
                "read_skill_resource",
                "run_skill_script",
            }
        )

    def __call__(self) -> FrameworkRegistry:
        registry = FrameworkRegistry()
        registry.set_mcp_client(McpSdkClient())
        backend = FileSystemBackend(self.settings)
        registry.skill_process_manager = SkillProcessManager(backend=backend)
        register_workspace_tools(
            registry,
            self.settings,
            download_base_path=self.service_base_url or "/api/backend/downloads",
        )
        register_pdf_tools(registry, self.settings)
        registry.browser_manager = BrowserManager(self.settings)
        register_browser_tools(registry, self.settings, registry.browser_manager)
        registry.register_local_tool(
            "get_current_time",
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "Return the current UTC date and time.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            handler=lambda _args, _context: datetime.now(timezone.utc).isoformat(),
        )
        registry.register_local_tool(
            "ask_user",
            {
                "type": "function",
                "function": {
                    "name": "ask_user",
                    "description": "Pause the current run and ask the user structured questions.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "questions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "header": {"type": "string"},
                                        "question": {"type": "string"},
                                        "options": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "label": {"type": "string"},
                                                    "description": {"type": "string"},
                                                },
                                                "required": ["label"],
                                            },
                                        },
                                    },
                                    "required": ["header", "question"],
                                },
                            },
                        },
                        "required": ["questions"],
                    },
                },
            },
            handler=_ask_user,
        )
        registry.register_local_tool(
            "milo_decision_matrix",
            {
                "type": "function",
                "function": {
                    "name": "milo_decision_matrix",
                    "description": (
                        "Calculate weighted option scores from supplied criteria and "
                        "scores. Never choose the scores for the user."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "criteria": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "weight": {"type": "number"},
                                    },
                                    "required": ["name", "weight"],
                                },
                            },
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "scores": {
                                            "type": "object",
                                            "additionalProperties": {"type": "number"},
                                        },
                                    },
                                    "required": ["name", "scores"],
                                },
                            },
                        },
                        "required": ["criteria", "options"],
                    },
                },
            },
            handler=lambda args, _context: calculate_weighted_options(
                args.get("criteria", []), args.get("options", [])
            ),
        )
        registry.register_local_tool(
            "milo_validate_task_state",
            {
                "type": "function",
                "function": {
                    "name": "milo_validate_task_state",
                    "description": (
                        "Check a task-state object for required sections and sources "
                        "on confirmed facts."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"state": {"type": "object"}},
                        "required": ["state"],
                    },
                },
            },
            handler=lambda args, _context: validate_task_state(args.get("state", {})),
        )
        for spec in self.skill_loader.discover_local():
            registry.register_manifest_skill(spec)
        registry.sync_skill_enabled_states(self.store.skill_states())
        if registry.manifest_skills:
            register_skill_meta_tools(registry, self.settings, backend)
        return registry


def _ask_user(args: dict[str, object], _context: object) -> UserInputRequest:
    raw = args.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("ask_user requires questions")
    questions: list[UserQuestion] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("ask_user question must be an object")
        normalized = dict(item)
        if isinstance(normalized.get("options"), list):
            normalized["options"] = [
                UserQuestionOption.model_validate(option).model_dump(mode="python")
                for option in normalized["options"]
                if isinstance(option, dict)
            ]
        questions.append(UserQuestion.model_validate(normalized))
    return UserInputRequest(
        id=uuid4().hex,
        tool_name="ask_user",
        title=str(args.get("title") or "Additional input required"),
        questions=questions,
    )
