from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import anyio
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    local_tools: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=500.0)
    provider_extra: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    reasoning_level: Mapped[str] = mapped_column(String(32), nullable=False, default="none")


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
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserRow(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
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
    transcript_messages_json: Mapped[list[dict[str, Any]]] = mapped_column(
        "transcript_messages",
        JSONB,
        nullable=False,
        default=list,
    )
    activity_json: Mapped[list[dict[str, Any]]] = mapped_column("activity", JSONB, nullable=False, default=list)
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
