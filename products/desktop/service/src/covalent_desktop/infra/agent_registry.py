"""Desktop composition for locally available Agent tools, skills, and MCP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from covalent_agent_kit.mcp.client import McpSdkClient
from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_agent_kit.skills.loader import SkillLoader
from covalent_agent_kit.skills.meta_tools import register_skill_meta_tools
from covalent_contracts.skill import ManifestSkillSpec


@dataclass
class DesktopSkillSettings:
    root: Path
    skills_directories: str | None = None

    def local_skill_directories(self) -> list[Path]:
        return [self.root]

    def managed_skill_directory(self, category: str) -> Path:
        return self.root / category


class DesktopRegistryFactory:
    def __init__(self, data_dir: Path) -> None:
        self.skill_loader = SkillLoader(DesktopSkillSettings(data_dir / "skills"))

    def available_skills(self) -> list[str]:
        return sorted(spec.name for spec in self._supported_skills())

    def _supported_skills(self) -> list[ManifestSkillSpec]:
        # Executable skills need process and permission composition, which the
        # Desktop service does not yet expose. Only instruction bundles are offered.
        return [
            spec
            for spec in self.skill_loader.discover_local()
            if not spec.is_executable
            and not spec.scripts
            and not spec.tools
            and not spec.references
        ]

    def __call__(self) -> FrameworkRegistry:
        registry = FrameworkRegistry()
        registry.set_mcp_client(McpSdkClient())
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
        for spec in self._supported_skills():
            registry.register_manifest_skill(spec)
        if registry.manifest_skills:
            register_skill_meta_tools(registry)
        return registry
