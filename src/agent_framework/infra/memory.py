from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_framework.core.types import Message
from agent_framework.infra.db import ChatSessionRow


SessionTitleSource = Literal["auto", "manual"]


class ChatTranscriptMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class ChatActivityItem(BaseModel):
    id: str
    title: str
    payload: Any = None


class ChatSessionSummary(BaseModel):
    id: str
    title: str
    title_source: SessionTitleSource = "auto"
    agent_name: str | None = None
    preview_text: str = ""
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ChatSessionRecord(ChatSessionSummary):
    memory_messages: list[Message] = Field(default_factory=list)
    messages: list[ChatTranscriptMessage] = Field(default_factory=list)
    activity: list[ChatActivityItem] = Field(default_factory=list)


class SessionStore(ABC):
    @abstractmethod
    async def load_messages(self, session_id: str) -> list[Message]:
        raise NotImplementedError

    @abstractmethod
    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(self) -> list[ChatSessionSummary]:
        raise NotImplementedError

    @abstractmethod
    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        raise NotImplementedError

    @abstractmethod
    async def update_title(self, session_id: str, title: str, title_source: SessionTitleSource = "manual") -> ChatSessionRecord:
        raise NotImplementedError

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSessionRecord] = {}

    def _default_record(self, session_id: str) -> ChatSessionRecord:
        now = datetime.now(UTC)
        return ChatSessionRecord(
            id=session_id,
            title="New conversation",
            title_source="auto",
            created_at=now,
            updated_at=now,
        )

    def _ensure_record(self, session_id: str) -> ChatSessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            record = self._default_record(session_id)
            self._sessions[session_id] = record
        return record

    async def load_messages(self, session_id: str) -> list[Message]:
        record = self._sessions.get(session_id)
        if record is None:
            return []
        return [message.model_copy(deep=True) for message in record.memory_messages]

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        record = self._ensure_record(session_id)
        record.memory_messages = [message.model_copy(deep=True) for message in messages]
        record.updated_at = datetime.now(UTC)

    async def list_sessions(self) -> list[ChatSessionSummary]:
        summaries = [ChatSessionSummary.model_validate(record.model_dump()) for record in self._sessions.values()]
        return sorted(summaries, key=lambda record: record.updated_at, reverse=True)

    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        record = self._sessions.get(session_id)
        return record.model_copy(deep=True) if record is not None else None

    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        next_record = record.model_copy(deep=True)
        if next_record.id in self._sessions:
            next_record.created_at = self._sessions[next_record.id].created_at
        next_record.updated_at = datetime.now(UTC)
        self._sessions[next_record.id] = next_record
        return next_record.model_copy(deep=True)

    async def update_title(self, session_id: str, title: str, title_source: SessionTitleSource = "manual") -> ChatSessionRecord:
        record = self._ensure_record(session_id)
        record.title = title
        record.title_source = title_source
        record.updated_at = datetime.now(UTC)
        return record.model_copy(deep=True)

    async def delete_session(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


class PersistentSessionStore(SessionStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_messages(self, session_id: str) -> list[Message]:
        async with self._session_factory() as session:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return []
            return [Message.model_validate(item) for item in row.memory_messages_json]

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    row = ChatSessionRow(id=session_id)
                    session.add(row)
                row.memory_messages_json = [message.model_dump(mode="json") for message in messages]

    async def list_sessions(self) -> list[ChatSessionSummary]:
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(ChatSessionRow).order_by(desc(ChatSessionRow.updated_at))))
        return [self._summary_from_row(row) for row in rows]

    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        async with self._session_factory() as session:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return None
            return self._record_from_row(row)

    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ChatSessionRow, record.id)
                if row is None:
                    row = ChatSessionRow(id=record.id)
                    session.add(row)
                row.title = record.title
                row.title_source = record.title_source
                row.agent_name = record.agent_name
                row.preview_text = record.preview_text
                row.memory_messages_json = [message.model_dump(mode="json") for message in record.memory_messages]
                row.transcript_messages_json = [message.model_dump(mode="json") for message in record.messages]
                row.activity_json = [item.model_dump(mode="json") for item in record.activity]
            await session.refresh(row)
            return self._record_from_row(row)

    async def update_title(self, session_id: str, title: str, title_source: SessionTitleSource = "manual") -> ChatSessionRecord:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    raise KeyError(session_id)
                row.title = title
                row.title_source = title_source
            await session.refresh(row)
            return self._record_from_row(row)

    async def delete_session(self, session_id: str) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    return False
                await session.delete(row)
                return True

    @staticmethod
    def _summary_from_row(row: ChatSessionRow) -> ChatSessionSummary:
        return ChatSessionSummary(
            id=row.id,
            title=row.title,
            title_source=row.title_source,
            agent_name=row.agent_name,
            preview_text=row.preview_text,
            message_count=len(row.transcript_messages_json or []),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    def _record_from_row(cls, row: ChatSessionRow) -> ChatSessionRecord:
        return ChatSessionRecord(
            **cls._summary_from_row(row).model_dump(),
            memory_messages=[Message.model_validate(item) for item in row.memory_messages_json],
            messages=[ChatTranscriptMessage.model_validate(item) for item in row.transcript_messages_json],
            activity=[ChatActivityItem.model_validate(item) for item in row.activity_json],
        )
