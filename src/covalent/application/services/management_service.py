"""Agent / provider / config-document / management helpers.

Extracted from ``app.py``. Module-level imports reach into ``_shared``,
``_skill_helpers`` and ``_runtime_apply`` — the one-way DAG is:
``_shared <- _skill_helpers <- _runtime_apply <- _config_helpers``.
``_skill_helpers`` never imports this module at module level (only lazily).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from covalent.application.errors import (ForbiddenError, InvalidInputError, NotFoundError)
from covalent.application._utils import RESOURCE_METADATA_FIELDS, _dedupe_strings, _new_chat_item_id
from covalent.application.audit import RequestMetadata, record_audit
from covalent.application.principal import Principal as ConsolePrincipalContext
from covalent.application.services.skill_service import (
    _build_skill_management_export_payload,
    _import_skill_management_payload,
)
from covalent.application.services.runtime_apply import _apply_runtime_config
from covalent.application.principal import ApiPrincipal
from covalent.application.schemas import (
    ConfigDocumentResponse,
    LocalToolSummaryResponse,
    ManagementExportFormat,
    ManagementImportResponse,
    ManagementKind,
    PublicationRequestResponse,
)
from covalent.core.agent import AgentSpec
from covalent.core.shell_tools import RUN_SHELL_TOOL, register_shell_tool
from covalent.core.types import Capability, RunContext, UserInputRequest, UserQuestion, UserQuestionOption
from covalent.core.workspace_tools import register_workspace_tools
from covalent.infra.config_store import ConfigKind, ConfigStore, PersistedAgentConfig, PersistedSkillSourceConfig
from covalent.infra.db import AgentRow, DatabaseManager, McpServerRow, ProviderRow, SkillSourceRow
from covalent.infra.memory import ChatSessionRecord
from covalent.infra.settings import AppSettings
from covalent.mcp.client import McpSdkClient
from covalent.mcp.spec import McpServerConfig, McpToolReference
from covalent.model.base import ProviderConfig
from covalent.model.factory import default_provider_config
from covalent.registry.registry import FrameworkRegistry
from covalent.runtime.backend import ExecutionBackend
from covalent.skills.loader import SkillLoader, normalize_git_source_payload
from covalent.skills.meta_tools import register_skill_meta_tools

logger = logging.getLogger(__name__)

LEGACY_REASONING_SKILL_NAME = "general_reasoning"

WORKSPACE_AGENT_TOOLS = (
    "list_workspace_files",
    "read_workspace_file",
    "search_workspace_files",
    "edit_workspace_file",
    "write_workspace_file",
    "create_workspace_directory",
    "copy_workspace_entry",
    "move_workspace_entry",
    "delete_workspace_entry",
    "zip_workspace_entries",
    "unzip_workspace_archive",
    "publish_downloadable_file",
)

BUILTIN_AGENT_TOOLS = ("get_current_time", "ask_user", *WORKSPACE_AGENT_TOOLS)

DEFAULT_AGENT_LOCAL_TOOLS = ("get_current_time",)

def _default_agent_local_tools(settings: AppSettings | None) -> list[str]:
    if settings is not None and not settings.enable_builtin_tools:
        return []
    return list(DEFAULT_AGENT_LOCAL_TOOLS)

def _normalize_agent_payload_item(item: dict[str, object], settings: AppSettings | None) -> dict[str, object]:
    normalized = dict(item)
    legacy_reasoning_skill_name = settings.reasoning_skill_name if settings is not None else LEGACY_REASONING_SKILL_NAME
    skills = _dedupe_strings([str(value) for value in normalized.get("skills", []) if isinstance(value, str)])
    local_tools = [t for t in _dedupe_strings([str(value) for value in normalized.get("local_tools", []) if isinstance(value, str)]) if t != "echo"]
    reasoning_prompt_raw = normalized.get("reasoning_prompt")
    reasoning_prompt = reasoning_prompt_raw.strip() if isinstance(reasoning_prompt_raw, str) else ""
    reasoning_level_raw = normalized.get("reasoning_level")
    reasoning_level = reasoning_level_raw.strip().lower() if isinstance(reasoning_level_raw, str) else "none"
    if not reasoning_level:
        reasoning_level = "none"

    if legacy_reasoning_skill_name in skills:
        skills = [skill for skill in skills if skill != legacy_reasoning_skill_name]
        if not reasoning_prompt and settings is not None:
            reasoning_prompt = settings.reasoning_skill_instructions

    normalized["skills"] = skills
    if "local_tools" not in item:
        local_tools = _dedupe_strings(local_tools + _default_agent_local_tools(settings))
    normalized["local_tools"] = local_tools
    allowed_outbound = _dedupe_strings([str(value) for value in normalized.get("allowed_outbound", []) if isinstance(value, str)])
    normalized["allowed_outbound"] = allowed_outbound
    normalized["reasoning_prompt"] = reasoning_prompt
    normalized["reasoning_level"] = reasoning_level
    return normalized

async def build_registry(
    settings: AppSettings,
    config_store: ConfigStore,
    backend: ExecutionBackend | None = None,
) -> tuple[FrameworkRegistry, SkillLoader, list[dict[str, object]]]:
    settings.ensure_managed_skill_directories()
    registry = FrameworkRegistry()
    register_skill_meta_tools(registry, settings, backend)
    mcp_payload = await config_store.ensure_document("mcp", _seed_mcp_payload(settings))
    mcp_servers = _parse_mcp_servers(mcp_payload)
    if settings.enable_builtin_tools:
        register_builtin_tools(registry, settings, backend)

    if settings.mcp_enabled:
        registry.set_mcp_client(McpSdkClient())
        for server in mcp_servers:
            registry.register_mcp_server(server)

    provider_config = await _resolve_default_provider(settings, config_store)
    agent_payload = await config_store.ensure_document(
        "agents",
        _seed_agent_payload(settings, provider_config, mcp_servers),
    )
    for agent in _build_agent_specs(agent_payload, provider_config, mcp_servers, settings, mcp_payload=mcp_payload):
        registry.register_agent(agent)

    loader = SkillLoader(settings)
    skill_source_payload = await config_store.ensure_document("skill_sources", _seed_skill_source_payload(settings))
    manifest_skills = loader.discover_local()
    for spec in manifest_skills:
        registry.register_manifest_skill(spec)
        logger.info("Loaded manifest skill '%s' (v%s) from %s", spec.name, spec.version, spec.source_dir)

    return registry, loader, skill_source_payload

def register_builtin_tools(registry: FrameworkRegistry, settings: AppSettings, backend: ExecutionBackend | None = None) -> None:
    register_workspace_tools(registry, settings)
    register_shell_tool(registry, settings, backend)
    registry.register_local_tool(
        "get_current_time",
        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": "Returns the current UTC timestamp.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        handler=lambda _args, _ctx: datetime.now(UTC).isoformat(),
    )
    registry.register_local_tool(
        "ask_user",
        {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": "Pause the current agent run and ask the user one or more structured questions.",
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
                                    "message": {"type": "string"},
                                    "multiSelect": {"type": "boolean"},
                                    "allowFreeformInput": {"type": "boolean"},
                                    "maxSelections": {"type": "integer", "minimum": 1},
                                    "options": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {"type": "string"},
                                                "description": {"type": "string"},
                                                "recommended": {"type": "boolean"},
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
        handler=_ask_user_handler,
    )

def _ask_user_handler(args: dict[str, Any], _ctx: RunContext | None) -> UserInputRequest:
    raw_questions = args.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("ask_user requires a non-empty 'questions' array")

    questions: list[UserQuestion] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            raise ValueError("ask_user questions must be objects")
        normalized_item = dict(item)
        if "multiSelect" in normalized_item:
            normalized_item["multi_select"] = normalized_item.pop("multiSelect")
        if "allowFreeformInput" in normalized_item:
            normalized_item["allow_freeform_input"] = normalized_item.pop("allowFreeformInput")
        if "maxSelections" in normalized_item:
            normalized_item["max_selections"] = normalized_item.pop("maxSelections")
        if isinstance(normalized_item.get("options"), list):
            normalized_item["options"] = [
                UserQuestionOption.model_validate(option).model_dump(mode="python")
                for option in normalized_item["options"]
                if isinstance(option, dict)
            ]
        questions.append(UserQuestion.model_validate(normalized_item))

    title = str(args.get("title") or "Additional input required").strip() or "Additional input required"
    return UserInputRequest(
        id=_new_chat_item_id("question"),
        tool_name="ask_user",
        title=title,
        questions=questions,
    )

def _available_local_tool_summaries(
    registry: FrameworkRegistry,
    settings: AppSettings,
) -> list[LocalToolSummaryResponse]:
    default_tools = set(_default_agent_local_tools(settings))
    summaries: list[LocalToolSummaryResponse] = []
    # The curated built-in tools, plus the sandbox shell tool when it's registered
    # (sandbox backend + flag on) — so operators can grant it per-agent in the
    # console without it ever showing up under filesystem / flag-off.
    candidate_names = list(BUILTIN_AGENT_TOOLS)
    if RUN_SHELL_TOOL in registry.local_tools and RUN_SHELL_TOOL not in candidate_names:
        candidate_names.append(RUN_SHELL_TOOL)
    for name in candidate_names:
        tool = registry.local_tools.get(name)
        if tool is None:
            continue
        function_payload = tool.schema.get("function", {}) if isinstance(tool.schema, dict) else {}
        description = function_payload.get("description") if isinstance(function_payload, dict) else None
        summaries.append(
            LocalToolSummaryResponse(
                name=name,
                description=description.strip() if isinstance(description, str) and description.strip() else None,
                enabled_by_default=name in default_tools,
            )
        )
    return summaries

async def _ensure_api_principal_can_invoke_agent(
    db_manager: DatabaseManager,
    principal: ApiPrincipal,
    agent_name: str,
) -> None:
    async with db_manager.session_factory() as session:
        row = await session.get(AgentRow, agent_name)
        if row is None:
            return
        if row.owner_user_id in {None, "", principal.user_id}:
            return
        if row.visibility == "public" and row.publication_status == "approved":
            return
    raise ForbiddenError(f"Token is not allowed to invoke agent: {agent_name}")

def _resource_display_name(row: object) -> str:
    return str(getattr(row, "display_name", None) or getattr(row, "name"))

def _visible_resource_clause(model: Any, user_id: str) -> Any:
    """SQL filter: a resource is visible to ``user_id`` if they own it OR it is
    public+approved. Used in ``.where(...)`` for agents/mcp/skill-sources.
    Admin scoping is handled by the caller (they pass a broad clause or none).
    """
    return (model.owner_user_id == user_id) | (
        (model.visibility == "public") & (model.publication_status == "approved")
    )

def _principal_can_access_resource(principal: ConsolePrincipalContext, row: object) -> bool:
    """In-memory access ladder for a resource row owned by a console principal.
    Returns True if the principal owns the row (same user+workspace) or the row
    is public+approved (including the legacy owner-less case).
    """
    if getattr(row, "owner_user_id", None) == principal.user_id and getattr(row, "workspace_id", None) == principal.workspace_id:
        return True
    if getattr(row, "owner_user_id", None) in {None, ""} and getattr(row, "visibility", None) == "public" and getattr(row, "publication_status", None) == "approved":
        return True
    if getattr(row, "visibility", None) == "public" and getattr(row, "publication_status", None) == "approved":
        return True
    return False

def _pick_agent_row_for_principal(
    rows: list[AgentRow],
    *,
    user_id: str,
    workspace_id: str | None = None,
) -> AgentRow | None:
    for row in rows:
        if row.owner_user_id == user_id and (workspace_id is None or row.workspace_id == workspace_id):
            return row
    for row in rows:
        if row.owner_user_id in {None, ""} and row.visibility == "public" and row.publication_status == "approved":
            return row
    for row in rows:
        if row.visibility == "public" and row.publication_status == "approved":
            return row
    return None

async def _resolve_api_agent_name(
    db_manager: DatabaseManager,
    principal: ApiPrincipal,
    agent_name: str,
) -> str:
    async with db_manager.session_factory() as session:
        row = await session.get(AgentRow, agent_name)
        if row is None:
            rows = list(await session.scalars(
                select(AgentRow).where(
                    AgentRow.display_name == agent_name,
                    _visible_resource_clause(AgentRow, principal.user_id),
                )
            ))
            row = _pick_agent_row_for_principal(rows, user_id=principal.user_id, workspace_id=principal.workspace_id)
        if row is None:
            raise NotFoundError(f"Unknown agent: {agent_name}")
        return row.name

def _ensure_console_principal_can_access_session(
    principal: ConsolePrincipalContext,
    record: ChatSessionRecord,
) -> None:
    if principal.is_admin:
        return
    if record.owner_user_id == principal.user_id and record.workspace_id == principal.workspace_id:
        return
    raise NotFoundError(f"Unknown session: {record.id}")

async def _resolve_console_agent_name(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    agent_name: str,
) -> str:
    async with db_manager.session_factory() as session:
        row = await session.get(AgentRow, agent_name)
        if row is None:
            rows = list(await session.scalars(
                select(AgentRow).where(
                    AgentRow.display_name == agent_name,
                    _visible_resource_clause(AgentRow, principal.user_id),
                )
            ))
            row = _pick_agent_row_for_principal(rows, user_id=principal.user_id, workspace_id=principal.workspace_id)
        if row is None:
            raise NotFoundError(f"Unknown agent: {agent_name}")
        return row.name

async def _ensure_console_principal_can_access_agent(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    agent_name: str,
) -> None:
    if principal.is_admin:
        return

    async with db_manager.session_factory() as session:
        row = await session.get(AgentRow, agent_name)
        if row is None:
            return
        if _principal_can_access_resource(principal, row):
            return
    raise NotFoundError(f"Unknown agent: {agent_name}")

async def _ensure_console_principal_can_access_mcp_server(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    server_name: str,
) -> None:
    if principal.is_admin:
        return

    async with db_manager.session_factory() as session:
        row = await session.get(McpServerRow, server_name)
        if row is None:
            rows = list(
                await session.scalars(
                    select(McpServerRow).where(
                        McpServerRow.display_name == server_name,
                        _visible_resource_clause(McpServerRow, principal.user_id),
                    )
                )
            )
            row = _pick_resource_row_for_principal(rows, principal)
        if row is None:
            raise NotFoundError(f"Unknown MCP server: {server_name}")
        if _principal_can_access_resource(principal, row):
            return
    raise NotFoundError(f"Unknown MCP server: {server_name}")

def _publication_response(kind: ConfigKind, row: object, name: str) -> PublicationRequestResponse:
    return PublicationRequestResponse(
        kind=kind,
        name=name,
        visibility=str(getattr(row, "visibility", "private") or "private"),
        publication_status=str(getattr(row, "publication_status", "draft") or "draft"),
    )

def _pick_resource_row_for_principal(
    rows: list[object],
    principal: ConsolePrincipalContext | None,
) -> object | None:
    if not rows:
        return None
    if principal is not None and not principal.is_admin:
        for row in rows:
            if getattr(row, "owner_user_id", None) == principal.user_id and getattr(row, "workspace_id", None) == principal.workspace_id:
                return row
    for row in rows:
        if getattr(row, "publication_status", None) == "pending":
            return row
    for row in rows:
        if getattr(row, "visibility", None) == "public" and getattr(row, "publication_status", None) == "approved":
            return row
    return rows[0]

async def _find_resource_row(
    session: AsyncSession,
    kind: ConfigKind,
    resource_name: str,
    principal: ConsolePrincipalContext | None = None,
) -> tuple[object, str]:
    if kind == "agents":
        row = await session.get(AgentRow, resource_name)
        if row is None:
            rows = list(await session.scalars(select(AgentRow).where(AgentRow.display_name == resource_name)))
            row = _pick_resource_row_for_principal(rows, principal)
        if row is None:
            raise NotFoundError(f"Unknown agent: {resource_name}")
        return row, _resource_display_name(row)

    if kind == "mcp":
        row = await session.get(McpServerRow, resource_name)
        if row is None:
            rows = list(await session.scalars(select(McpServerRow).where(McpServerRow.display_name == resource_name)))
            row = _pick_resource_row_for_principal(rows, principal)
        if row is None:
            raise NotFoundError(f"Unknown MCP server: {resource_name}")
        return row, _resource_display_name(row)

    if kind == "providers":
        row = await session.scalar(select(ProviderRow).where(ProviderRow.name == resource_name))
        if row is None:
            rows = list(await session.scalars(select(ProviderRow).where(ProviderRow.display_name == resource_name)))
            row = _pick_resource_row_for_principal(rows, principal)
        if row is None:
            raise NotFoundError(f"Unknown provider: {resource_name}")
        return row, _resource_display_name(row)

    row = await session.scalar(select(SkillSourceRow).where(SkillSourceRow.name == resource_name))
    if row is None and resource_name.isdigit():
        row = await session.get(SkillSourceRow, int(resource_name))
    if row is None:
        raise NotFoundError(f"Unknown skill source: {resource_name}")
    return row, row.name or str(row.id)

def _ensure_console_principal_owns_resource(principal: ConsolePrincipalContext, row: object, resource_name: str) -> None:
    if principal.is_admin:
        return
    owner_user_id = getattr(row, "owner_user_id", None)
    workspace_id = getattr(row, "workspace_id", None)
    if owner_user_id == principal.user_id and workspace_id == principal.workspace_id:
        return
    raise NotFoundError(f"Unknown resource: {resource_name}")

async def _request_resource_publication(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    kind: ConfigKind,
    resource_name: str,
    request_metadata: RequestMetadata | None = None,
) -> PublicationRequestResponse:
    async with db_manager.session_factory() as session:
        async with session.begin():
            row, display_name = await _find_resource_row(session, kind, resource_name, principal)
            _ensure_console_principal_owns_resource(principal, row, resource_name)
            if getattr(row, "owner_user_id", None) in {None, ""}:
                setattr(row, "owner_user_id", principal.user_id)
            if getattr(row, "workspace_id", None) in {None, ""}:
                setattr(row, "workspace_id", principal.workspace_id)
            setattr(row, "visibility", "private")
            setattr(row, "publication_status", "pending")
            setattr(row, "publication_requested_at", datetime.now(UTC))
            setattr(row, "publication_reviewed_at", None)
            setattr(row, "publication_reviewed_by_user_id", None)
            response = _publication_response(kind, row, display_name)
    await record_audit(
        db_manager,
        action="publication.requested",
        target_type=kind,
        target_id=response.name,
        principal=principal,
        request_metadata=request_metadata,
        metadata={"visibility": response.visibility, "publication_status": response.publication_status},
    )
    return response

async def _review_resource_publication(
    db_manager: DatabaseManager,
    principal: ConsolePrincipalContext,
    kind: ConfigKind,
    resource_name: str,
    status: Literal["approved", "rejected"],
    request_metadata: RequestMetadata | None = None,
) -> PublicationRequestResponse:
    if not principal.is_admin:
        raise ForbiddenError("Only admins can review publication requests")

    async with db_manager.session_factory() as session:
        async with session.begin():
            row, display_name = await _find_resource_row(session, kind, resource_name, principal)
            if status == "approved":
                setattr(row, "visibility", "public")
                setattr(row, "publication_status", "approved")
            else:
                setattr(row, "visibility", "private")
                setattr(row, "publication_status", "rejected")
            setattr(row, "publication_reviewed_at", datetime.now(UTC))
            setattr(row, "publication_reviewed_by_user_id", principal.user_id)
            response = _publication_response(kind, row, display_name)
    await record_audit(
        db_manager,
        action=f"publication.{status}",
        target_type=kind,
        target_id=response.name,
        principal=principal,
        request_metadata=request_metadata,
        metadata={"visibility": response.visibility, "publication_status": response.publication_status},
    )
    return response

def _normalize_management_kind(kind: str) -> ManagementKind:
    if kind not in {"agents", "mcp", "skills"}:
        raise NotFoundError(f"Unknown management kind: {kind}")
    return kind

def _normalize_management_export_format(value: str) -> ManagementExportFormat:
    normalized = value.strip().lower()
    if normalized not in {"yaml", "json"}:
        raise InvalidInputError(f"Unsupported export format: {value}")
    return normalized  # type: ignore[return-value]

async def _build_management_export_payload(
    registry: FrameworkRegistry,
    settings: AppSettings,
    config_store: ConfigStore,
    kind: ManagementKind,
    principal: ConsolePrincipalContext,
) -> tuple[dict[str, Any], int]:
    exported_at = datetime.now(UTC).isoformat()

    if kind in {"agents", "mcp"}:
        raw_items = await config_store.get_document(kind, principal.config)
        items = [
            _normalize_agent_payload_item(item, settings) if kind == "agents" else item
            for item in raw_items
        ]
        return {
            "version": 1,
            "kind": kind,
            "exported_at": exported_at,
            "items": items,
        }, len(items)

    payload = await _build_skill_management_export_payload(registry, settings, config_store, principal)
    return payload, len(payload.get("items", []))

def _serialize_management_export_payload(
    payload: dict[str, Any],
    export_format: ManagementExportFormat,
) -> str:
    if export_format == "yaml":
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    return f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"

async def _import_management_payload(
    db_manager: DatabaseManager,
    registry: FrameworkRegistry,
    config_store: ConfigStore,
    settings: AppSettings,
    loader: SkillLoader,
    execution_backend: ExecutionBackend,
    kind: ManagementKind,
    raw_text: str,
    file_name: str | None,
    principal: ConsolePrincipalContext,
    request_metadata: RequestMetadata | None = None,
) -> ManagementImportResponse:
    parsed = _parse_management_upload(raw_text, file_name)

    if kind in {"agents", "mcp"}:
        raw_items = _extract_management_items(kind, parsed)
        validated = _validate_config_payload(kind, raw_items, settings)
        saved = await config_store.save_document(kind, validated, principal=principal.config)
        await _apply_runtime_config(registry, config_store, settings, loader, execution_backend, kind, await config_store.get_document(kind))
        label = "agents" if kind == "agents" else "MCP services"
        response = ManagementImportResponse(
            kind=kind,
            imported_items=len(validated),
            applied_items=len(saved),
            summary=f"Imported {len(saved)} {label}.",
        )
        await record_audit(
            db_manager,
            action="management.imported",
            target_type=kind,
            target_id=file_name,
            principal=principal,
            request_metadata=request_metadata,
            metadata={"imported_items": response.imported_items, "applied_items": response.applied_items},
        )
        return response

    response = await _import_skill_management_payload(registry, config_store, settings, loader, execution_backend, parsed, principal)
    await record_audit(
        db_manager,
        action="management.imported",
        target_type=kind,
        target_id=file_name,
        principal=principal,
        request_metadata=request_metadata,
        metadata={"imported_items": response.imported_items, "applied_items": response.applied_items},
    )
    return response

def _parse_management_upload(raw_text: str, file_name: str | None) -> Any:
    if not raw_text.strip():
        raise InvalidInputError("Imported file is empty")
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        target_name = file_name or "uploaded file"
        raise InvalidInputError(f"Could not parse {target_name}: {exc}") from exc
    if parsed is None:
        raise InvalidInputError("Imported file did not contain any configuration data")
    return parsed

def _extract_management_items(kind: ConfigKind, payload: Any) -> list[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise InvalidInputError("Imported configuration must be a YAML/JSON object or array")

    payload_kind = payload.get("kind")
    if isinstance(payload_kind, str) and payload_kind and payload_kind != kind:
        raise InvalidInputError(f"Imported file is for '{payload_kind}', not '{kind}'")

    items = payload.get("items")
    if items is None and isinstance(payload.get("data"), list):
        items = payload.get("data")
    if not isinstance(items, list):
        raise InvalidInputError("Imported configuration must include an 'items' array")
    return items

def _normalize_config_kind(kind: str) -> ConfigKind:
    if kind not in {"agents", "mcp", "skill_sources", "providers"}:
        raise NotFoundError(f"Unknown config kind: {kind}")
    return kind

def _config_document_response(kind: ConfigKind, payload: list[dict[str, object]], settings: AppSettings) -> ConfigDocumentResponse:
    label_map = {"agents": "Agents", "mcp": "MCP Servers", "skill_sources": "Skill Sources", "providers": "Providers"}
    normalized_payload = [
        _normalize_agent_payload_item(item, settings) if kind == "agents" else item
        for item in payload
    ]
    if kind == "providers":
        normalized_payload = [_mask_provider_api_key(item) for item in normalized_payload]
    return ConfigDocumentResponse(
        kind=kind,
        label=label_map[kind],
        filePath=f"postgres://config/{kind}",
        raw=f"{json.dumps(normalized_payload, ensure_ascii=False, indent=2)}\n",
        exampleRaw=_example_config_raw(kind, settings),
        data=normalized_payload,
    )

def _example_config_raw(kind: ConfigKind, settings: AppSettings) -> str:
    if kind == "providers":
        example = {
            "name": "my-provider",
            "provider_type": "openai_compatible",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-...",
            "default_model": "gpt-4.1",
            "position": 0,
        }
        return f"{json.dumps([example], ensure_ascii=False, indent=2)}\n"
    raw_map = {
        "agents": settings.agents_json,
        "mcp": settings.mcp_servers_json,
        "skill_sources": settings.skill_sources_json,
    }
    raw = raw_map[kind]
    payload = json.loads(raw) if raw else []
    if kind == "agents":
        payload = _validate_config_payload("agents", payload or [], settings)
    return f"{json.dumps(payload or [], ensure_ascii=False, indent=2)}\n"

def _seed_mcp_payload(settings: AppSettings) -> list[dict[str, object]]:
    if not settings.mcp_servers_json:
        return []
    return json.loads(settings.mcp_servers_json)

def _seed_skill_source_payload(settings: AppSettings) -> list[dict[str, object]]:
    if not settings.skill_sources_json:
        return []
    raw = json.loads(settings.skill_sources_json)
    return _validate_config_payload("skill_sources", raw)

def _seed_agent_payload(
    settings: AppSettings,
    provider_config: ProviderConfig,
    mcp_servers: list[McpServerConfig],
) -> list[dict[str, object]]:
    raw = json.loads(settings.agents_json) if settings.agents_json else None
    if raw is not None:
        return _validate_config_payload("agents", raw, settings)

    default_item = PersistedAgentConfig(
        name="default",
        description=settings.agent_description,
        system_prompt=settings.agent_system_prompt,
        reasoning_prompt=settings.reasoning_skill_instructions,
        provider=provider_config,
        skills=[],
        local_tools=_default_agent_local_tools(settings),
        mcp_servers=[server.name for server in mcp_servers],
        capabilities={Capability.CHAT, Capability.REACT, Capability.TOOL_CALLING, Capability.STREAMING},
        max_iterations=settings.default_max_iterations,
    )
    return [default_item.model_dump(mode="json")]

def _validate_config_payload(kind: ConfigKind, payload: list[object], settings: AppSettings | None = None) -> list[dict[str, object]]:
    if kind == "mcp":
        normalized_servers: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise InvalidInputError("MCP server entries must be JSON objects")
            normalized = McpServerConfig.model_validate(item).model_dump(mode="json")
            normalized.update({field: item[field] for field in RESOURCE_METADATA_FIELDS if field in item})
            normalized_servers.append(normalized)
        return normalized_servers

    if kind == "providers":
        from covalent.infra.config_store import PersistedProviderConfig
        normalized_providers = [PersistedProviderConfig.model_validate(item).model_dump(mode="json") for item in payload]
        default_model_names = [
            str(item.get("name") or "")
            for item in normalized_providers
            if str(item.get("default_model") or "").strip()
        ]
        if len(default_model_names) > 1:
            raise InvalidInputError(
                "Only one provider may declare a default_model. "
                f"Found: {', '.join(default_model_names)}"
            )

        if default_model_names:
            default_name = default_model_names[0]
            return [
                {
                    **item,
                    "default_model": str(item.get("default_model") or "").strip(),
                    "is_default": str(item.get("name") or "") == default_name,
                }
                for item in normalized_providers
            ]

        legacy_default_names = [
            str(item.get("name") or "")
            for item in normalized_providers
            if bool(item.get("is_default"))
        ]
        if len(legacy_default_names) > 1:
            raise InvalidInputError(
                "Only one provider may be marked as default. "
                f"Found: {', '.join(legacy_default_names)}"
            )

        return [
            {
                **item,
                "default_model": str(item.get("default_model") or "").strip(),
            }
            for item in normalized_providers
        ]

    if kind == "skill_sources":
        normalized_sources: list[dict[str, object]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise InvalidInputError("Skill source entries must be JSON objects")
            normalized = normalize_git_source_payload(item)
            if normalized is None:
                raise InvalidInputError("Only git skill sources are currently supported")
            normalized.update({field: item[field] for field in RESOURCE_METADATA_FIELDS if field in item})
            normalized_sources.append(PersistedSkillSourceConfig.model_validate(normalized).model_dump(mode="json"))
        return normalized_sources

    seen_names: set[str] = set()
    normalized: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise InvalidInputError("Agent config entries must be JSON objects")
        normalized_item = dict(item)
        mcp_refs = normalized_item.get("mcp_servers", [])
        if isinstance(mcp_refs, list):
            normalized_item["mcp_servers"] = [
                ref if isinstance(ref, str) else str(ref.get("name"))
                for ref in mcp_refs
                if isinstance(ref, str) or (isinstance(ref, dict) and ref.get("name"))
            ]
        tool_refs = normalized_item.get("mcp_tools", [])
        if isinstance(tool_refs, list):
            normalized_item["mcp_tools"] = [
                McpToolReference.model_validate(tool_ref).model_dump(mode="json")
                for tool_ref in tool_refs
                if isinstance(tool_ref, dict)
            ]
        normalized_item = _normalize_agent_payload_item(normalized_item, settings)
        agent = PersistedAgentConfig.model_validate(normalized_item)
        if agent.name in seen_names:
            raise InvalidInputError(f"Duplicate agent name: {agent.name}")
        referenced_servers = {tool.server_name for tool in agent.mcp_tools}
        missing_server_refs = sorted(referenced_servers.difference(agent.mcp_servers))
        if missing_server_refs:
            raise InvalidInputError(
                    f"Agent '{agent.name}' has MCP tool selections for unselected servers: {', '.join(missing_server_refs)}"
        )
        seen_names.add(agent.name)
        normalized.append(agent.model_dump(mode="json"))
    return normalized

def _extract_agent_renames(metadata: object) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}

    raw_renames = metadata.get("agent_renames")
    if not isinstance(raw_renames, list):
        return {}

    rename_map: dict[str, str] = {}
    for item in raw_renames:
        if not isinstance(item, dict):
            continue
        old_name = item.get("old_name")
        new_name = item.get("new_name")
        if not isinstance(old_name, str) or not isinstance(new_name, str):
            continue
        old_normalized = old_name.strip()
        new_normalized = new_name.strip()
        if not old_normalized or not new_normalized or old_normalized == new_normalized:
            continue
        rename_map[old_normalized] = new_normalized

    return rename_map

def _parse_mcp_servers(payload: list[dict[str, object]]) -> list[McpServerConfig]:
    return [McpServerConfig.model_validate(_runtime_named_resource_payload(item)) for item in payload]

def _runtime_named_resource_payload(item: dict[str, object]) -> dict[str, object]:
    runtime_item = dict(item)
    internal_name = runtime_item.get("internal_name")
    if isinstance(internal_name, str) and internal_name.strip():
        runtime_item["name"] = internal_name.strip()
    return runtime_item

def _runtime_agent_payload_item(
    item: dict[str, object],
    *,
    mcp_internal_by_public: dict[str, str] | None = None,
    agent_internal_by_public: dict[str, str] | None = None,
) -> dict[str, object]:
    runtime_item = _runtime_named_resource_payload(item)
    mcp_internal_by_public = mcp_internal_by_public or {}
    agent_internal_by_public = agent_internal_by_public or {}

    if mcp_internal_by_public:
        runtime_item["mcp_servers"] = [
            mcp_internal_by_public.get(server_name, server_name)
            for server_name in runtime_item.get("mcp_servers", [])
            if isinstance(server_name, str)
        ]
        runtime_item["mcp_tools"] = [
            {
                **tool_ref,
                "server_name": mcp_internal_by_public.get(tool_ref.get("server_name"), tool_ref.get("server_name")),
            }
            for tool_ref in runtime_item.get("mcp_tools", [])
            if isinstance(tool_ref, dict)
        ]

    if agent_internal_by_public:
        runtime_item["delegate_agents"] = [
            agent_internal_by_public.get(agent_name, agent_name)
            for agent_name in runtime_item.get("delegate_agents", [])
            if isinstance(agent_name, str)
        ]
    return runtime_item

def _runtime_internal_name_map(payload: list[dict[str, object]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in payload:
        name = item.get("name")
        internal_name = item.get("internal_name")
        if not isinstance(name, str) or not name:
            continue
        resolved_name = internal_name.strip() if isinstance(internal_name, str) and internal_name.strip() else name
        mapping[name] = resolved_name
        mapping[resolved_name] = resolved_name
    return mapping

def _build_agent_specs(
    payload: list[dict[str, object]],
    provider_config: ProviderConfig,
    mcp_servers: list[McpServerConfig],
    settings: AppSettings,
    *,
    mcp_payload: list[dict[str, object]] | None = None,
) -> list[AgentSpec]:
    mcp_by_name = {server.name: server for server in mcp_servers}
    mcp_internal_by_public = _runtime_internal_name_map(mcp_payload or [])
    agent_internal_by_public = _runtime_internal_name_map(payload)
    agents: list[AgentSpec] = []
    for item in payload:
        if item.get("enabled", True) is False:
            continue
        runtime_item = _runtime_agent_payload_item(
            item,
            mcp_internal_by_public=mcp_internal_by_public,
            agent_internal_by_public=agent_internal_by_public,
        )
        persisted = PersistedAgentConfig.model_validate(_normalize_agent_payload_item(runtime_item, settings))
        resolved_mcp = [mcp_by_name[name] for name in persisted.mcp_servers if name in mcp_by_name]
        agents.append(
            AgentSpec(
                name=persisted.name,
                description=persisted.description,
                system_prompt=persisted.system_prompt,
                reasoning_prompt=persisted.reasoning_prompt,
                reasoning_level=persisted.reasoning_level,
                provider=_merge_provider_config(persisted.provider, provider_config),
                skills=persisted.skills,
                local_tools=[t for t in _dedupe_strings(persisted.local_tools) if t != "echo"],
                allowed_outbound=_dedupe_strings(getattr(persisted, "allowed_outbound", []) or []),
                delegate_agents=persisted.delegate_agents,
                mcp_servers=resolved_mcp,
                mcp_tools=persisted.mcp_tools,
                capabilities=persisted.capabilities,
                max_iterations=persisted.max_iterations,
                metadata=dict(persisted.metadata),
            )
        )
    return agents

async def _resolve_default_provider(
    settings: AppSettings,
    config_store: ConfigStore,
    providers_payload: list[dict[str, object]] | None = None,
) -> ProviderConfig:
    if providers_payload is None:
        try:
            providers_payload = await config_store.get_document("providers")
        except Exception:
            # Don't crash agent startup, but surface the DB failure — otherwise a
            # transient config-store error silently routes traffic to the
            # fallback default provider/model instead of the configured one.
            logger.warning(
                "Failed to load providers from config store; falling back to default provider",
                exc_info=True,
            )
            providers_payload = []

    from covalent.infra.config_store import PersistedProviderConfig

    for item in providers_payload or []:
        default_model = str(item.get("default_model") or "").strip()
        if default_model and item.get("base_url"):
            cfg = PersistedProviderConfig.model_validate(item)
            return ProviderConfig(
                provider=cfg.provider_type,
                model=default_model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout_seconds=settings.request_timeout_seconds,
            )

    for item in providers_payload or []:
        if item.get("is_default") and item.get("base_url"):
            cfg = PersistedProviderConfig.model_validate(item)
            return ProviderConfig(
                provider=cfg.provider_type,
                model=settings.default_model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout_seconds=settings.request_timeout_seconds,
            )

    return default_provider_config(settings)

def _format_api_key_masked(key: str) -> str:
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:5]}{'•' * (len(key) - 8)}{key[-3:]}"

def _mask_provider_api_key(item: dict[str, object]) -> dict[str, object]:
    masked = dict(item)
    if masked.get("api_key"):
        key = str(masked["api_key"])
        masked["has_api_key"] = bool(key)
        masked["api_key_masked"] = _format_api_key_masked(key) if key else None
        masked["api_key"] = None
    else:
        masked["has_api_key"] = False
        masked["api_key_masked"] = None
    return masked

def _merge_provider_config(
    provider: ProviderConfig,
    default_provider: ProviderConfig,
) -> ProviderConfig:
    return ProviderConfig(
        provider=provider.provider or default_provider.provider,
        model=provider.model or default_provider.model,
        api_key=provider.api_key or default_provider.api_key,
        base_url=provider.base_url or default_provider.base_url,
        timeout_seconds=provider.timeout_seconds or default_provider.timeout_seconds,
        extra={**default_provider.extra, **provider.extra},
    )

