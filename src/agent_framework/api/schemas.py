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
            if len(value) > 32768:
                raise ValueError("string input must be at most 32768 characters; use structured content for large attachments")
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


class ProviderSummaryResponse(BaseModel):
    provider: str
    model: str
    base_url: str | None = None
    timeout_seconds: float


class AgentSummaryResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    reasoning_prompt: str
    skills: list[str]
    local_tools: list[str]
    delegate_agents: list[str]
    capabilities: set[Capability]
    max_iterations: int
    provider: ProviderSummaryResponse


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


class SeedSyncRequest(BaseModel):
    kinds: list[Literal["agents", "mcp", "skill_sources"]] = Field(
        default_factory=lambda: ["mcp", "skill_sources", "agents"]
    )
    overwrite: bool = False


class SeedSyncResult(BaseModel):
    kind: Literal["agents", "mcp", "skill_sources"]
    status: Literal["seeded", "overwritten", "skipped", "empty_seed"]
    items: int


class SeedSyncResponse(BaseModel):
    results: list[SeedSyncResult] = Field(default_factory=list)


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
    kind: Literal["agents", "mcp", "skill_sources"]
    label: str
    filePath: str = "database"
    exists: bool = True
    raw: str = "[]\n"
    exampleRaw: str = "[]\n"
    data: list[dict[str, Any]] = Field(default_factory=list)
    lastModified: str | None = None


class ConfigDocumentUpdateRequest(BaseModel):
    raw: str
