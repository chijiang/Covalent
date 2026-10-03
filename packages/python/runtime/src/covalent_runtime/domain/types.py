from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from covalent_contracts.messages import (
    Capability as Capability,
    ToolCall as ToolCall,
    UserQuestionOption as UserQuestionOption,
    UserQuestion as UserQuestion,
    UserInputRequest as UserInputRequest,
    ParentInputRequest as ParentInputRequest,
    DelegateRunStatus as DelegateRunStatus,
    DelegateRunResult as DelegateRunResult,
    ResumedToolResult as ResumedToolResult,
    ToolResult as ToolResult,
    Message as Message,
    GenerationRequest as GenerationRequest,
    TokenUsage as TokenUsage,
    GenerationResponse as GenerationResponse,
    PromptContent as PromptContent,
)

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
