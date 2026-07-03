from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import anyio
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
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
    provider_type: Mapped[str] = mapped_column(String(100), nullable=False, default="openai_compatible")
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ChatSessionRow(TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
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
