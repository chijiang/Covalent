from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field
from covalent_contracts.messages import DelegateRunStatus, ParentInputRequest

class DelegateRunRecord(BaseModel):
    id: str
    session_id: str | None = None
    execution_scope_id: str
    workspace_scope_id: str
    workspace_id: str | None = None
    root_agent_name: str
    parent_agent_name: str
    parent_delegate_run_id: str | None = None
    delegate_agent_name: str
    origin_tool_call_id: str | None = None
    status: DelegateRunStatus = DelegateRunStatus.CREATED
    pending_request: ParentInputRequest | None = None
    latest_output: str = ""
    summary: str = ""
    error: dict[str, Any] = Field(default_factory=dict)
    release_reason: str = ""
    version: int = 1
    created_at: datetime
    last_activity_at: datetime
    released_at: datetime | None = None
    expires_at: datetime | None = None


