from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import anyio
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


_T = TypeVar("_T")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class McpServerRow(TimestampMixin, Base):
    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    publication_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_by_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    args: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)


class McpServerEnvVarRow(Base):
    __tablename__ = "mcp_server_env_vars"

    server_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("mcp_servers.name", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class AgentRow(TimestampMixin, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    publication_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_by_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    local_tools: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    allowed_outbound: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=500.0)
    provider_extra: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    reasoning_level: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    sandbox_profile_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("sandbox_profiles.id", ondelete="RESTRICT"),
        nullable=True,
    )


class SkillSourceRow(TimestampMixin, Base):
    __tablename__ = "skill_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    publication_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_by_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="git")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="github_synced")
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subdir: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkillStateRow(TimestampMixin, Base):
    __tablename__ = "skill_states"

    skill_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AgentCapabilityRow(Base):
    __tablename__ = "agent_capabilities"

    agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentSkillRow(Base):
    __tablename__ = "agent_skills"

    agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentDelegateRow(Base):
    __tablename__ = "agent_delegates"

    agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    delegate_agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentMcpServerRow(Base):
    __tablename__ = "agent_mcp_servers"

    agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    server_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("mcp_servers.name", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentMcpToolRow(Base):
    __tablename__ = "agent_mcp_tools"

    agent_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("agents.name", ondelete="CASCADE"),
        primary_key=True,
    )
    server_name: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("mcp_servers.name", ondelete="CASCADE"),
        primary_key=True,
    )
    tool_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ProviderRow(TimestampMixin, Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="public")
    publication_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    publication_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_reviewed_by_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    provider_type: Mapped[str] = mapped_column(String(100), nullable=False, default="openai_compatible")
    apih_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserRow(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferences_json: Mapped[dict[str, Any]] = mapped_column("preferences", JSONB, nullable=False, default=dict)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    auth_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceRow(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceMemberRow(TimestampMixin, Base):
    __tablename__ = "workspace_members"

    workspace_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")


class ApiTokenRow(TimestampMixin, Base):
    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    policy_json: Mapped[dict[str, Any]] = mapped_column("policy", JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentAccessGrantRow(TimestampMixin, Base):
    __tablename__ = "agent_access_grants"
    __table_args__ = (
        UniqueConstraint("workspace_id", "agent_name", "subject_type", "subject_id", name="uq_agent_access_grant_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), ForeignKey("agents.name", ondelete="CASCADE"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)


class AgentRunLogRow(Base):
    __tablename__ = "agent_run_logs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    token_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    usage_json: Mapped[dict[str, Any]] = mapped_column("usage", JSONB, nullable=False, default=dict)
    error_json: Mapped[dict[str, Any]] = mapped_column("error", JSONB, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_token_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SandboxProfileRow(TimestampMixin, Base):
    """Administrator-managed sandbox environment template.

    ``workspace_id`` keeps its tenant/organization meaning here (NULL = global);
    it is not the filesystem ``workspace_scope_id`` used at runtime.
    """

    __tablename__ = "sandbox_profiles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_sandbox_profiles_workspace_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    pull_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="if_not_present")
    keepalive_command: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    runtime_capabilities: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    memory_limit: Mapped[str] = mapped_column(String(32), nullable=False)
    pids_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    cpus: Mapped[float] = mapped_column(Float, nullable=False)
    tmpfs_size: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    validated_image_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_image_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SandboxInstanceRow(TimestampMixin, Base):
    """Logical sandbox binding for one (execution scope, agent) pair.

    The row survives container teardown; the Docker container is recreated
    lazily from ``spec_snapshot`` (an immutable profile-revision snapshot).
    ``session_id`` is NULL for stateless ``scope_kind='run'`` bindings, which
    are removed by the run cleanup path.
    """

    __tablename__ = "sandbox_instances"
    __table_args__ = (
        UniqueConstraint("execution_scope_id", "agent_name", name="uq_sandbox_instances_scope_agent"),
        CheckConstraint("scope_kind IN ('session', 'run')", name="ck_sandbox_instances_scope_kind"),
        CheckConstraint(
            "(scope_kind = 'session' AND session_id IS NOT NULL) OR (scope_kind = 'run' AND session_id IS NULL)",
            name="ck_sandbox_instances_scope_session",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    execution_scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sandbox_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    profile_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allowed_outbound_snapshot: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DelegateRunRow(TimestampMixin, Base):
    """One logical delegate run (stateful subagent lifecycle)."""

    __tablename__ = "delegate_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','running','waiting_parent','idle','released','cancelled','failed','expired')",
            name="ck_delegate_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    execution_scope_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    workspace_scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    root_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_delegate_run_id: Mapped[str | None] = mapped_column(
        String(96),
        ForeignKey("delegate_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    delegate_agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    origin_tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pending_request_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    latest_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    release_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DelegateMessageRow(Base):
    """Ordered private delegate memory; mirrors core.types.Message."""

    __tablename__ = "delegate_messages"
    __table_args__ = (
        UniqueConstraint("delegate_run_id", "position", name="uq_delegate_messages_run_position"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    delegate_run_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("delegate_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # full Message.model_dump(mode="json"); sibling columns are queryable projections
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    reasoning_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatSessionRow(TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True)
    created_by_token_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="New conversation")
    title_source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    preview_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory_messages_json: Mapped[list[dict[str, Any]]] = mapped_column("memory_messages", JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_chat_messages_session_id_position"),
    )


class ChatActivityRow(Base):
    __tablename__ = "chat_activity"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(64), nullable=False)
    # Display payload kept small on every list read; raw model blobs live in
    # raw_payload and are served on demand via the activity detail endpoint.
    payload: Mapped[Any] = mapped_column(JSONB, nullable=True)
    raw_payload: Mapped[Any] = mapped_column(JSONB, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_chat_activity_session_id_position"),
    )


class ChatRunRow(TimestampMixin, Base):
    """One durable chat turn execution (background worker driven)."""

    __tablename__ = "chat_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','cancelling','completed','cancelled','failed')",
            name="ck_chat_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column("input", JSONB, nullable=False, default=dict)
    error_json: Mapped[dict[str, Any]] = mapped_column("error", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatRunEventRow(Base):
    """Replayable SSE event log; ``position`` is the per-run SSE event id."""

    __tablename__ = "chat_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_chat_run_events_run_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


async def run_session_operation(
    session_factory: async_sessionmaker[AsyncSession],
    operation: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    with anyio.CancelScope(shield=True):
        async with session_factory() as session:
            return await operation(session)


class DatabaseManager:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def dispose(self) -> None:
        await self.engine.dispose()
