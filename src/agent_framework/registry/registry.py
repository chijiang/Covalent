from __future__ import annotations

import base64
import logging
from typing import Any

from agent_framework.core.agent import AgentSpec
from agent_framework.core.tooling import ToolDefinition, ToolHandler
from agent_framework.core.types import RunContext, ToolCall, ToolResult, UserInputRequest
from agent_framework.mcp.adapter import McpClient
from agent_framework.mcp.spec import McpServerConfig
from agent_framework.model.base import ModelAdapter, ProviderConfig
from agent_framework.model.factory import build_provider
from agent_framework.skills.exceptions import SkillProcessError, SkillStartupError
from agent_framework.skills.meta_tools import LIST_SKILL_FILES_TOOL, READ_SKILL_RESOURCE_TOOL, RUN_SKILL_SCRIPT_TOOL
from agent_framework.skills.process import SkillProcessManager
from agent_framework.skills.spec import ManifestSkillSpec, SkillSpec

logger = logging.getLogger(__name__)


class FrameworkRegistry:
    def __init__(self) -> None:
        self.agents: dict[str, AgentSpec] = {}
        self.skills: dict[str, SkillSpec] = {}
        self.manifest_skills: dict[str, ManifestSkillSpec] = {}
        self.skill_enabled: dict[str, bool] = {}
        self._skill_tool_map: dict[str, str] = {}
        self.model_providers: dict[str, ModelAdapter] = {}
        self.mcp_servers: dict[str, McpServerConfig] = {}
        self.local_tools: dict[str, ToolDefinition] = {}
        self.mcp_client: McpClient | None = None
        self.skill_process_manager: SkillProcessManager | None = None

    def register_agent(self, spec: AgentSpec) -> None:
        self.agents[spec.name] = spec

    def register_skill(self, spec: SkillSpec) -> None:
        self.skills[spec.name] = spec
        self.skill_enabled.setdefault(spec.name, True)

    def register_manifest_skill(self, spec: ManifestSkillSpec) -> None:
        self.manifest_skills[spec.name] = spec
        self.register_skill(spec.to_skill_spec())
        for tool_decl in spec.tools:
            if tool_decl.name in self._skill_tool_map:
                existing = self._skill_tool_map[tool_decl.name]
                if existing != spec.name:
                    logger.warning(
                        "Tool name collision: '%s' claimed by both '%s' and '%s'; keeping '%s'",
                        tool_decl.name,
                        existing,
                        spec.name,
                        existing,
                    )
                    continue
            self._skill_tool_map[tool_decl.name] = spec.name

    def unregister_skill(self, name: str) -> ManifestSkillSpec | None:
        manifest = self.manifest_skills.pop(name, None)
        if manifest:
            for tool_decl in manifest.tools:
                if self._skill_tool_map.get(tool_decl.name) == name:
                    self._skill_tool_map.pop(tool_decl.name, None)
        self.skills.pop(name, None)
        self.skill_enabled.pop(name, None)
        return manifest

    def sync_skill_enabled_states(self, enabled_by_name: dict[str, bool]) -> None:
        current_skill_names = set(self.skills)
        for name in list(self.skill_enabled):
            if name not in current_skill_names:
                self.skill_enabled.pop(name, None)
        for name in current_skill_names:
            self.skill_enabled[name] = enabled_by_name.get(name, True)

    def set_skill_enabled(self, name: str, enabled: bool) -> None:
        self.skill_enabled[name] = enabled

    def is_skill_enabled(self, name: str) -> bool:
        return self.skill_enabled.get(name, True)

    def register_mcp_server(self, config: McpServerConfig) -> None:
        self.mcp_servers[config.name] = config

    def register_local_tool(self, name: str, tool_schema: dict[str, Any], handler: ToolHandler | None = None) -> None:
        self.local_tools[name] = ToolDefinition(name=name, schema=tool_schema, handler=handler)

    def set_mcp_client(self, client: McpClient) -> None:
        self.mcp_client = client

    def get_agent(self, name: str) -> AgentSpec:
        return self.agents[name]

    def get_skill(self, name: str) -> SkillSpec:
        return self.skills[name]

    def get_model_provider(self, config: ProviderConfig) -> ModelAdapter:
        cache_key = config.cache_key()
        adapter = self.model_providers.get(cache_key)
        if adapter is None:
            adapter = build_provider(config)
            self.model_providers[cache_key] = adapter
        return adapter

    async def resolve_tools_for_agent(self, agent: AgentSpec) -> list[dict[str, Any]]:
        tool_schemas: list[dict[str, Any]] = []
        local_tool_names: set[str] = set(agent.local_tools)
        allowed_mcp_tools: dict[str, set[str]] = {}

        for tool_ref in agent.mcp_tools:
            allowed_mcp_tools.setdefault(tool_ref.server_name, set()).add(tool_ref.tool_name)

        for skill_name in agent.skills:
            if not self.is_skill_enabled(skill_name):
                continue
            manifest = self.manifest_skills.get(skill_name)
            if manifest:
                for tool_decl in manifest.tools:
                    tool_schemas.append(tool_decl.to_openai_tool_schema())
                local_tool_names.update(manifest.references)
                if manifest.resource_files or manifest.scripts:
                    local_tool_names.add(LIST_SKILL_FILES_TOOL)
                if manifest.resource_files:
                    local_tool_names.add(READ_SKILL_RESOURCE_TOOL)
                if manifest.scripts:
                    local_tool_names.add(RUN_SKILL_SCRIPT_TOOL)
                continue
            skill = self.skills.get(skill_name)
            if skill:
                local_tool_names.update(skill.tools)

        for name in local_tool_names:
            if name in self.local_tools:
                tool_schemas.append(self.local_tools[name].schema)

        if self.mcp_client:
            for server in agent.mcp_servers:
                tool_schemas.extend(await self._export_mcp_tools(server, allowed_mcp_tools.get(server.name)))

        return tool_schemas

    async def execute_tool_call(
        self,
        agent: AgentSpec,
        tool_call: ToolCall,
        context: RunContext | None = None,
    ) -> ToolResult:
        skill_name = self._skill_tool_map.get(tool_call.name)
        if skill_name:
            return await self._execute_skill_tool(skill_name, tool_call, context)

        if tool_call.name in self.local_tools:
            try:
                content = await self.local_tools[tool_call.name].invoke(tool_call.arguments, context)
                if isinstance(content, UserInputRequest):
                    request = content.model_copy(
                        update={
                            "tool_call_id": content.tool_call_id or tool_call.id,
                            "tool_name": content.tool_name or tool_call.name,
                        }
                    )
                    return ToolResult(
                        name=tool_call.name,
                        content="Input required",
                        tool_call_id=tool_call.id,
                        input_request=request,
                    )
                return ToolResult(name=tool_call.name, content=content, tool_call_id=tool_call.id)
            except Exception as exc:
                return ToolResult(name=tool_call.name, content=str(exc), tool_call_id=tool_call.id, is_error=True)

        server_name, remote_tool_name = self._decode_mcp_tool_name(tool_call.name)
        if server_name and self.mcp_client:
            server = self.mcp_servers.get(server_name)
            if server is None:
                return ToolResult(
                    name=tool_call.name,
                    content=f"Unknown MCP server: {server_name}",
                    tool_call_id=tool_call.id,
                    is_error=True,
                )
            try:
                result = await self.mcp_client.call_tool(server, remote_tool_name, tool_call.arguments)
                result.tool_call_id = tool_call.id
                return result
            except Exception as exc:
                return ToolResult(
                    name=tool_call.name,
                    content=str(exc),
                    tool_call_id=tool_call.id,
                    is_error=True,
                )

        return ToolResult(
            name=tool_call.name,
            content=f"Unknown tool: {tool_call.name}",
            tool_call_id=tool_call.id,
            is_error=True,
        )

    async def _execute_skill_tool(
        self,
        skill_name: str,
        tool_call: ToolCall,
        context: RunContext | None,
    ) -> ToolResult:
        if not self.is_skill_enabled(skill_name):
            return ToolResult(
                name=tool_call.name,
                content=f"Skill '{skill_name}' is disabled",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        spec = self.manifest_skills.get(skill_name)
        if spec is None or self.skill_process_manager is None:
            return ToolResult(
                name=tool_call.name,
                content=f"Skill '{skill_name}' is not registered or has no process manager",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        handle = None
        try:
            handle = await self.skill_process_manager.acquire(spec)
            result_data = await handle.send_request(
                "call_tool",
                {"name": tool_call.name, "arguments": tool_call.arguments},
            )
            result_data = result_data or {}
            return ToolResult(
                name=tool_call.name,
                content=result_data.get("content", ""),
                tool_call_id=tool_call.id,
                is_error=bool(result_data.get("is_error", False)),
            )
        except (SkillProcessError, SkillStartupError) as exc:
            return ToolResult(
                name=tool_call.name,
                content=f"Skill '{skill_name}' error: {exc}",
                tool_call_id=tool_call.id,
                is_error=True,
            )
        finally:
            if handle is not None:
                await self.skill_process_manager.release(handle)

    async def _export_mcp_tools(
        self,
        server: McpServerConfig,
        allowed_tool_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.mcp_client is None:
            raise RuntimeError("MCP client is not initialized")
        exported: list[dict[str, Any]] = []
        for tool in await self.mcp_client.list_tools(server):
            if allowed_tool_names is not None and tool.tool_name not in allowed_tool_names:
                continue
            parameters = tool.input_schema
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            exported.append(
                {
                    "type": "function",
                    "function": {
                        "name": self._encode_mcp_tool_name(server.name, tool.tool_name),
                        "description": tool.description or f"MCP tool '{tool.tool_name}' from server '{server.name}'",
                        "parameters": parameters,
                    },
                }
            )
        return exported

    @staticmethod
    def _encode_mcp_tool_name(server_name: str, tool_name: str) -> str:
        return f"mcp__{FrameworkRegistry._encode_name_part(server_name)}__{FrameworkRegistry._encode_name_part(tool_name)}"

    @staticmethod
    def _decode_mcp_tool_name(name: str) -> tuple[str | None, str]:
        if not name.startswith("mcp__"):
            return None, name
        try:
            _, server_name, tool_name = name.split("__", 2)
            return FrameworkRegistry._decode_name_part(server_name), FrameworkRegistry._decode_name_part(tool_name)
        except (ValueError, UnicodeDecodeError):
            return None, name

    async def aclose(self) -> None:
        if self.skill_process_manager:
            await self.skill_process_manager.stop()
        for adapter in self.model_providers.values():
            await adapter.aclose()

    def has_executable_skills(self, *, enabled_only: bool = False) -> bool:
        return any(
            spec.is_executable and (not enabled_only or self.is_skill_enabled(spec.name))
            for spec in self.manifest_skills.values()
        )

    @staticmethod
    def _encode_name_part(value: str) -> str:
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
        return encoded.rstrip("=")

    @staticmethod
    def _decode_name_part(value: str) -> str:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
