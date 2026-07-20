from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from agent_framework.core.agent import AgentSpec
from agent_framework.core.shell_tools import RUN_SHELL_TOOL
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
        # tool name -> normalized parameter schema, populated in resolve_tools_for_agent
        # so execute_tool_call can repair stringified object/array arguments.
        self._tool_schemas: dict[str, dict[str, Any]] = {}

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

    @staticmethod
    def _normalize_lookup_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")

    def resolve_skill_name(self, name: str, *, manifest_only: bool = False) -> str | None:
        candidates = self.manifest_skills if manifest_only else self.skills
        raw_name = name.strip()
        if not raw_name:
            return None
        if raw_name in candidates:
            return raw_name

        lowered_matches = [candidate for candidate in candidates if candidate.lower() == raw_name.lower()]
        if len(lowered_matches) == 1:
            return lowered_matches[0]

        normalized = self._normalize_lookup_name(raw_name)
        if not normalized:
            return None
        normalized_matches = [
            candidate for candidate in candidates if self._normalize_lookup_name(candidate) == normalized
        ]
        if len(normalized_matches) == 1:
            return normalized_matches[0]
        return None

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

        # Cache each tool's parameter schema (standard pydantic form, sent to the model
        # unchanged) so execute_tool_call can repair stringified object/array arguments
        # before forwarding to the MCP server.
        for tool in tool_schemas:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                self._tool_schemas[function["name"]] = function.get("parameters") or {}
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

        canonical_mcp_name = self.normalize_mcp_tool_name(tool_call.name)
        server_name, remote_tool_name = self._decode_mcp_tool_name(canonical_mcp_name)
        if server_name and self.mcp_client:
            server = self.mcp_servers.get(server_name)
            if server is None:
                return ToolResult(
                    name=canonical_mcp_name,
                    content=f"Unknown MCP server: {server_name}",
                    tool_call_id=tool_call.id,
                    is_error=True,
                )
            try:
                arguments = FrameworkRegistry._coerce_arguments_to_schema(
                    tool_call.arguments, self._tool_schemas.get(canonical_mcp_name)
                )
                result = await self.mcp_client.call_tool(server, remote_tool_name, arguments)
                result.name = canonical_mcp_name
                result.tool_call_id = tool_call.id
                return result
            except Exception as exc:
                return ToolResult(
                    name=canonical_mcp_name,
                    content=str(exc),
                    tool_call_id=tool_call.id,
                    is_error=True,
                )

        # Fuzzy match: if the tool name was not found, try to match it against
        # known tool names. This handles common model mistakes such as:
        # - Double-encoded MCP names: mcp__query-server__mcp__cXV...__c2Vh...
        # - Slight misspellings: query_instancel -> query_instances
        match = self._fuzzy_match_tool_name(tool_call.name)
        if match is not None:
            corrected_call = ToolCall(
                id=tool_call.id,
                name=match,
                arguments=tool_call.arguments,
                raw=tool_call.raw,
            )
            return await self.execute_tool_call(agent, corrected_call, context)

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
            handle = await self.skill_process_manager.acquire(spec, context=context)
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
        try:
            tools = await self.mcp_client.list_tools(server)
        except Exception as exc:
            logger.warning("Skipping unavailable MCP server '%s': %s", server.name, exc)
            return exported
        for tool in tools:
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
    def _coerce_arguments_to_schema(arguments: Any, schema: Any) -> Any:
        """Best-effort repair of tool-call arguments against a JSON Schema.

        Some OpenAI-compatible models serialize object/array parameters as JSON
        strings. For each property the schema declares as object/array (directly or via
        a pydantic ``anyOf``/``oneOf`` Optional union), if the model supplied a JSON
        string we parse it back in place. Recurses into nested objects and arrays.
        Returns a new dict; the input is not mutated.
        """
        if not isinstance(arguments, dict) or not isinstance(schema, dict):
            return arguments
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return arguments
        return {
            key: FrameworkRegistry._coerce_value(value, properties.get(key))
            for key, value in arguments.items()
        }

    @staticmethod
    def _coerce_value(value: Any, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return value
        # Narrow pydantic Optional[T] unions (anyOf/oneOf with a null member) to the
        # concrete member so we still recognize object/array types on standard schemas.
        schema = FrameworkRegistry._narrow_schema(schema)
        if schema.get("type") == "object" or isinstance(schema.get("properties"), dict):
            if isinstance(value, str):
                parsed = FrameworkRegistry._try_parse_json(value, dict)
                if parsed is not None:
                    value = parsed
            if isinstance(value, dict):
                return FrameworkRegistry._coerce_arguments_to_schema(value, schema)
            return value
        if schema.get("type") == "array" or isinstance(schema.get("items"), dict):
            if isinstance(value, str):
                parsed = FrameworkRegistry._try_parse_json(value, list)
                if parsed is not None:
                    value = parsed
            if isinstance(value, list):
                item_schema = schema.get("items")
                return [FrameworkRegistry._coerce_value(item, item_schema) for item in value]
            return value
        return value

    @staticmethod
    def _narrow_schema(schema: Any) -> dict[str, Any]:
        """Resolve an ``anyOf``/``oneOf`` union (pydantic ``Optional[T]``) to its
        non-null member for type inspection. Non-union schemas are returned as-is.
        """
        if not isinstance(schema, dict):
            return schema  # type: ignore[return-value]
        for union_key in ("anyOf", "oneOf"):
            members = schema.get(union_key)
            if isinstance(members, list):
                non_null = [m for m in members if isinstance(m, dict) and m.get("type") != "null"]
                if non_null:
                    return non_null[0]
        return schema

    @staticmethod
    def _try_parse_json(text: str, expected: type) -> Any:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, expected) else None

    @staticmethod
    def _encode_mcp_tool_name(server_name: str, tool_name: str) -> str:
        return f"mcp__{FrameworkRegistry._encode_name_part(server_name)}__{FrameworkRegistry._encode_name_part(tool_name)}"

    @staticmethod
    def _decode_mcp_tool_name(name: str) -> tuple[str | None, str]:
        if not name.startswith("mcp__"):
            return None, name
        try:
            _, server_name, tool_name = name.split("__", 2)
            decoded_server_name = FrameworkRegistry._decode_name_part(server_name)
            decoded_tool_name = FrameworkRegistry._try_decode_name_part(tool_name)
            return decoded_server_name, decoded_tool_name if decoded_tool_name is not None else tool_name
        except (ValueError, UnicodeDecodeError):
            return None, name

    @staticmethod
    def normalize_mcp_tool_name(name: str) -> str:
        if not name.startswith("mcp__"):
            return name
        try:
            _, server_name, tool_name = name.split("__", 2)
        except ValueError:
            return name
        decoded_server_name = FrameworkRegistry._try_decode_name_part(server_name)
        if decoded_server_name is None:
            return name
        decoded_tool_name = FrameworkRegistry._try_decode_name_part(tool_name)
        return FrameworkRegistry._encode_mcp_tool_name(
            decoded_server_name,
            decoded_tool_name if decoded_tool_name is not None else tool_name,
        )

    @staticmethod
    def display_mcp_tool_name(name: str) -> str:
        normalized = FrameworkRegistry.normalize_mcp_tool_name(name)
        server_name, tool_name = FrameworkRegistry._decode_mcp_tool_name(normalized)
        if not server_name:
            return name
        return f"mcp__{server_name}__{tool_name}"

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

    @staticmethod
    def _try_decode_name_part(value: str) -> str | None:
        try:
            decoded = FrameworkRegistry._decode_name_part(value)
        except (ValueError, UnicodeDecodeError):
            return None
        return decoded if FrameworkRegistry._encode_name_part(decoded) == value.rstrip("=") else None

    def _fuzzy_match_tool_name(self, requested: str) -> str | None:
        """Try to resolve a misspelled or double-encoded tool name to a known tool.

        Handles cases where the model outputs:
        - Double-encoded MCP names: mcp__query-server__mcp__cXV...__c2Vh...
          where the remote tool part is itself an encoded MCP tool name.
        """
        if not requested.startswith("mcp__"):
            return None

        parts = requested.split("__", 2)
        if len(parts) < 3:
            return None

        server_raw = parts[1]
        tool_raw = parts[2]

        # Check if tool_raw itself looks like an MCP name (mcp__b64server__b64tool)
        if tool_raw.startswith("mcp__"):
            inner_parts = tool_raw.split("__", 2)
            if len(inner_parts) == 3:
                inner_server = self._try_decode_name_part(inner_parts[1])
                inner_tool = self._try_decode_name_part(inner_parts[2])
                if inner_server and inner_tool and inner_server in self.mcp_servers:
                    return self._encode_mcp_tool_name(inner_server, inner_tool)

        return None
