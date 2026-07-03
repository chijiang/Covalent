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

    def to_message(self) -> "Message":
        return Message(
            role="tool",
            content=self.content if isinstance(self.content, str) else str(self.content),
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


class GenerationResponse(BaseModel):
    output_text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    assistant_message: Message | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)
    usage: TokenUsage | None = None


class RunContext(BaseModel):
    agent_name: str
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
