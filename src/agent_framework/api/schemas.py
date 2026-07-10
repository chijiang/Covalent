from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agent_framework.core.types import Capability, PromptContent
from agent_framework.mcp.spec import McpServerConfig


class AgentRunRequest(BaseModel):
    input: PromptContent
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: PromptContent) -> PromptContent:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("input must not be empty")
            if len(value) > 1_000_000:
                raise ValueError("string input must be at most 1M characters; use structured content for large attachments")
            return value
        if not value:
            raise ValueError("input content list must not be empty")
        if any(not isinstance(item, dict) for item in value):
            raise ValueError("input content items must be objects")
        return value


class AgentRunResponse(BaseModel):
    agent: str
    output_text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None


class PublicAgentInvokeMemory(BaseModel):
    mode: Literal["none", "session"] = "none"
    session_id: str | None = None


class PublicAgentInvokeTrace(BaseModel):
    level: Literal["none", "steps", "debug"] = "steps"


class PublicAgentInvokeRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=255)
    input: PromptContent
    stream: bool = False
    memory: PublicAgentInvokeMemory = Field(default_factory=PublicAgentInvokeMemory)
    trace: PublicAgentInvokeTrace = Field(default_factory=PublicAgentInvokeTrace)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: PromptContent) -> PromptContent:
        return AgentRunRequest(input=value).input


class PublicAgentInvokeResponse(BaseModel):
    id: str
    agent: str
    memory_mode: Literal["none", "session"]
    session_id: str | None = None
    output_text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    created_at: datetime


class ConsoleLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class ConsoleRegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)
    display_name: str = Field(default="", max_length=255)
    workspace_name: str = Field(default="Default workspace", max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("email must be a valid email address")
        return normalized


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    user_id: str | None = None
    user_email: str = "admin@local"
    user_display_name: str = "Local Admin"
    workspace_id: str | None = None
    workspace_name: str = "Default workspace"
    workspace_slug: str = "default"
    scopes: list[str] = Field(default_factory=lambda: ["agent:invoke"])
    policy: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class ApiTokenSummaryResponse(BaseModel):
    id: str
    name: str
    user_id: str
    user_email: str
    workspace_id: str
    workspace_name: str
    token_prefix: str
    scopes: list[str]
    policy: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ApiTokenCreateResponse(ApiTokenSummaryResponse):
    token: str


class ConsoleUserResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str
    workspace_id: str
    workspace_name: str
    workspace_role: str


class ConsoleUserSummaryResponse(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str
    status: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_role: str | None = None
    created_at: datetime
    updated_at: datetime


class ConsoleUserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "disabled"] | None = None
    workspace_role: Literal["admin", "member"] | None = None


class AgentRunLogResponse(BaseModel):
    id: str
    user_id: str | None = None
    token_id: str | None = None
    workspace_id: str | None = None
    agent_name: str
    memory_mode: str
    session_id: str | None = None
    status: str
    latency_ms: int | None = None
    provider: str | None = None
    model: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditLogResponse(BaseModel):
    id: str
    actor_user_id: str | None = None
    actor_token_id: str | None = None
    workspace_id: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    outcome: str
    request_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PublicationReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]


class PublicationRequestResponse(BaseModel):
    kind: Literal["agents", "mcp", "skill_sources", "providers"]
    name: str
    visibility: str
    publication_status: str


class ProviderSummaryResponse(BaseModel):
    model: str
    timeout_seconds: float


class AgentSummaryResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    reasoning_prompt: str
    reasoning_level: str
    skills: list[str]
    local_tools: list[str]
    delegate_agents: list[str]
    capabilities: set[Capability]
    max_iterations: int
    provider: ProviderSummaryResponse


class LocalToolSummaryResponse(BaseModel):
    name: str
    description: str | None = None
    enabled_by_default: bool = False


class ChatSessionMessageResponse(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class AttachmentUploadItemResponse(BaseModel):
    name: str
    size: int
    content_type: str
    last_modified: int = 0
    workspace_path: str
    uploaded_at: datetime
    delivery_mode: Literal["parse", "workspace"] = "parse"
    kind: Literal["text", "image", "pdf", "binary"] = "binary"
    summary: str = ""
    model_prompt_text: str = ""
    model_content: list[dict[str, Any]] = Field(default_factory=list)
    page_count: int | None = None


class AttachmentUploadResponse(BaseModel):
    session_id: str
    files: list[AttachmentUploadItemResponse] = Field(default_factory=list)


class ChatSessionActivityResponse(BaseModel):
    id: str
    title: str
    payload: Any = None


class ChatSessionSummaryResponse(BaseModel):
    id: str
    title: str
    title_source: Literal["auto", "manual"] = "auto"
    agent_name: str | None = None
    preview_text: str = ""
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ChatSessionResponse(ChatSessionSummaryResponse):
    messages: list[ChatSessionMessageResponse] = Field(default_factory=list)
    activity: list[ChatSessionActivityResponse] = Field(default_factory=list)


class ChatSessionUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


# --- Skill management schemas ---


class SkillSummaryResponse(BaseModel):
    name: str
    version: str
    description: str
    source_type: Literal["local", "git"]
    category: Literal["built_in", "uploaded", "authored", "github_synced", "unknown"] = "unknown"
    source_dir: str | None = None
    runtime_type: Literal["python", "nodejs"] | None
    tools: list[str]
    references: list[str]
    enabled: bool
    publication_resource_name: str | None = None
    owner_user_id: str | None = None
    workspace_id: str | None = None
    visibility: Literal["private", "public"] = "public"
    publication_status: Literal["draft", "pending", "approved", "rejected"] = "approved"
    publication_requested_at: str | None = None
    publication_reviewed_at: str | None = None
    publication_reviewed_by_user_id: str | None = None


class SkillInstallRequest(BaseModel):
    source: str
    source_type: Literal["directory", "git"] | None = None
    ref: str | None = None
    name: str | None = None
    subdir: str | None = None
    category: Literal["built_in", "uploaded", "authored", "github_synced"] = "uploaded"


class SkillInstallResponse(BaseModel):
    name: str
    version: str
    description: str
    status: Literal["installed", "already_exists"]


class SkillPreviewFileResponse(BaseModel):
    path: str
    language: str
    content: str


class SkillPreviewResponse(BaseModel):
    name: str
    source_dir: str | None = None
    files: list[SkillPreviewFileResponse] = Field(default_factory=list)


class McpToolSummaryResponse(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class McpInspectRequest(BaseModel):
    server: McpServerConfig


class McpInspectResponse(BaseModel):
    server: McpServerConfig
    tools: list[McpToolSummaryResponse] = Field(default_factory=list)


class McpToolCallRequest(BaseModel):
    server: McpServerConfig
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpToolCallResponse(BaseModel):
    name: str
    content: Any
    is_error: bool = False


class ConfigDocumentResponse(BaseModel):
    kind: Literal["agents", "mcp", "skill_sources", "providers"]
    label: str
    filePath: str = "database"
    exists: bool = True
    raw: str = "[]\n"
    exampleRaw: str = "[]\n"
    data: list[dict[str, Any]] = Field(default_factory=list)
    lastModified: str | None = None


class ConfigDocumentUpdateRequest(BaseModel):
    raw: str
    metadata: dict[str, Any] = Field(default_factory=dict)


ManagementKind = Literal["agents", "mcp", "skills"]
ManagementExportFormat = Literal["yaml", "json"]


class ManagementExportResponse(BaseModel):
    kind: ManagementKind
    format: ManagementExportFormat
    file_name: str
    content_type: str
    content: str
    item_count: int = 0


class ManagementImportResponse(BaseModel):
    kind: ManagementKind
    imported_items: int = 0
    applied_items: int = 0
    summary: str
    warnings: list[str] = Field(default_factory=list)


class SkillManagementSourceResponse(BaseModel):
    type: Literal["built_in", "managed", "git", "inline", "unknown"]
    category: Literal["built_in", "uploaded", "authored", "github_synced", "unknown"] | None = None
    url: str | None = None
    ref: str | None = None
    subdir: str | None = None
    name: str | None = None


class SkillManagementItemResponse(BaseModel):
    name: str
    enabled: bool = True
    category: Literal["built_in", "uploaded", "authored", "github_synced", "unknown"] = "unknown"
    source_type: Literal["local", "git"] = "local"
    version: str = ""
    description: str = ""
    source: SkillManagementSourceResponse
