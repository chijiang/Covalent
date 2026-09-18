from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field
PromptContent = str | list[dict[str, Any]]


class Capability(str, Enum):
    CHAT = "chat"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    MCP = "mcp"
    REACT = "react"
    CHART = "chart"
    SUGGESTED_QUESTIONS = "suggested_questions"


class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class UserQuestionOption(BaseModel):
    label: str
    description: str | None = None
    recommended: bool = False


class UserQuestion(BaseModel):
    header: str
    question: str
    message: str | None = None
    multi_select: bool = False
    allow_freeform_input: bool = True
    max_selections: int | None = None
    options: list[UserQuestionOption] = Field(default_factory=list)


class UserInputRequest(BaseModel):
    id: str
    tool_call_id: str | None = None
    tool_name: str
    title: str = "Additional input required"
    questions: list[UserQuestion] = Field(default_factory=list)


class ParentInputRequest(BaseModel):
    id: str
    delegate_run_id: str
    tool_call_id: str | None = None
    tool_name: Literal["ask_parent"] = "ask_parent"
    title: str
    questions: list[UserQuestion] = Field(default_factory=list)


class DelegateRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_PARENT = "waiting_parent"
    IDLE = "idle"
    RELEASED = "released"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"


class DelegateRunResult(BaseModel):
    delegate_run_id: str
    agent_name: str
    status: DelegateRunStatus
    output: str = ""
    request: ParentInputRequest | None = None
    error: dict[str, Any] = Field(default_factory=dict)


class ResumedToolResult(BaseModel):
    tool_call_id: str | None = None
    tool_name: str
    request_id: str
    answers: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""


class ToolResult(BaseModel):
    name: str
    content: Any
    tool_call_id: str | None = None
    is_error: bool = False
    input_request: UserInputRequest | None = None
    parent_request: ParentInputRequest | None = None

    def to_message(self) -> "Message":
        return Message(
            role="tool",
            content=self.content if isinstance(self.content, (str, list)) else str(self.content),
            name=self.name,
            tool_call_id=self.tool_call_id,
        )


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Any
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_content: str | None = ''


class GenerationRequest(BaseModel):
    model: str
    messages: list[Message]
    system_prompt: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_level: str = "none"
    temperature: float = 0.0
    max_tokens: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 来自 prompt_tokens_details / completion_tokens_details；provider 未返回时为 None。
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None


class GenerationResponse(BaseModel):
    output_text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    assistant_message: Message | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)
    usage: TokenUsage | None = None
    suggestions: list[str] = Field(default_factory=list)


class RunContext(BaseModel):
    agent_name: str
    session_id: str | None = None
    # Explicit memory identity (stateful delegates): memory_scope_kind determines
    # whether this run has isolated memory; memory_scope_id names the scope;
    # delegate_run_id identifies this delegate run; parent_delegate_run_id
    # tracks the logical parent for nested delegates.
    memory_scope_kind: Literal["session", "delegate", "none"] = "session"
    memory_scope_id: str | None = None
    delegate_run_id: str | None = None
    parent_delegate_run_id: str | None = None
    # Execution identity (per-agent sandbox profiles). ``session_id`` drives
    # memory/trace persistence; ``execution_scope_id`` is the sandbox lifecycle
    # scope (chat session id, or run id for stateless invokes);
    # ``workspace_scope_id`` is the filesystem scope shared by collaborating
    # agents; ``sandbox_instance_id`` identifies this agent's logical sandbox.
    # All default to None for legacy paths until a binding resolver runs.
    execution_scope_id: str | None = None
    workspace_scope_id: str | None = None
    sandbox_instance_id: str | None = None
    # Tenant/organization identity for this run (profile visibility/default
    # resolution). Distinct from workspace_scope_id (a filesystem lifecycle key).
    workspace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # The execution backend for this run (injected so the workspace file tools
    # resolve the session workspace through it). None in tests/legacy paths ->
    # the tools fall back to settings-derived resolution.
    execution_backend: Any = None

    @property
    def memory_mode(self) -> Literal["session", "none"]:
        raw = self.metadata.get("memory_mode")
        return "none" if raw == "none" else "session"

    @property
    def sandbox_execution_key(self) -> str | None:
        """The key execution must be scoped to: this agent's sandbox instance
        when a binding has resolved, else the legacy session id."""
        return self.sandbox_instance_id or self.session_id
