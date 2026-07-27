from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_framework.core.types import Message
from agent_framework.infra.db import ChatActivityRow, ChatMessageRow, ChatSessionRow, run_session_operation


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
    owner_user_id: str | None = None
    workspace_id: str | None = None
    created_by_token_id: str | None = None
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
    async def list_sessions(self, *, owner_user_id: str | None = None, workspace_id: str | None = None) -> list[ChatSessionSummary]:
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

    async def list_sessions(self, *, owner_user_id: str | None = None, workspace_id: str | None = None) -> list[ChatSessionSummary]:
        summaries = [
            ChatSessionSummary.model_validate(record.model_dump())
            for record in self._sessions.values()
            if (owner_user_id is None or record.owner_user_id == owner_user_id)
            and (workspace_id is None or record.workspace_id == workspace_id)
        ]
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
        async def _load(session: AsyncSession) -> list[Message]:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return []
            return [Message.model_validate(item) for item in row.memory_messages_json]

        return await run_session_operation(self._session_factory, _load)

    async def save_messages(self, session_id: str, messages: list[Message]) -> None:
        async def _save(session: AsyncSession) -> None:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    row = ChatSessionRow(id=session_id)
                    session.add(row)
                row.memory_messages_json = [message.model_dump(mode="json") for message in messages]

        await run_session_operation(self._session_factory, _save)

    async def list_sessions(self, *, owner_user_id: str | None = None, workspace_id: str | None = None) -> list[ChatSessionSummary]:
        async def _list(session: AsyncSession) -> list[ChatSessionSummary]:
            stmt = select(ChatSessionRow).order_by(desc(ChatSessionRow.updated_at))
            if owner_user_id is not None:
                stmt = stmt.where(ChatSessionRow.owner_user_id == owner_user_id)
            if workspace_id is not None:
                stmt = stmt.where(ChatSessionRow.workspace_id == workspace_id)
            rows = list(await session.scalars(stmt))
            summaries: list[ChatSessionSummary] = []
            for row in rows:
                count = await self._count_messages(session, row.id)
                summaries.append(self._summary_from_row(row, message_count=count))
            return summaries

        return await run_session_operation(self._session_factory, _list)

    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        async def _get(session: AsyncSession) -> ChatSessionRecord | None:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return None
            return await self._record_from_row(session, row)

        return await run_session_operation(self._session_factory, _get)

    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        async def _save(session: AsyncSession) -> ChatSessionRecord:
            async with session.begin():
                row = await session.get(ChatSessionRow, record.id)
                if row is None:
                    row = ChatSessionRow(id=record.id)
                    session.add(row)
                if record.owner_user_id is not None:
                    row.owner_user_id = record.owner_user_id
                if record.workspace_id is not None:
                    row.workspace_id = record.workspace_id
                if record.created_by_token_id is not None:
                    row.created_by_token_id = record.created_by_token_id
                row.title = record.title
                row.title_source = record.title_source
                row.agent_name = record.agent_name
                row.preview_text = record.preview_text
                row.memory_messages_json = [
                    message.model_dump(mode="json") for message in record.memory_messages
                ]

                await session.execute(
                    delete(ChatMessageRow).where(ChatMessageRow.session_id == record.id)
                )
                for position, message in enumerate(record.messages):
                    session.add(
                        ChatMessageRow(
                            id=message.id,
                            session_id=record.id,
                            role=message.role,
                            content=message.content,
                            attachments=list(message.attachments),
                            position=position,
                        )
                    )
                if record.activity:
                    await session.execute(
                        pg_insert(ChatActivityRow)
                        .values(
                            [
                                {
                                    "id": item.id,
                                    "session_id": record.id,
                                    "title": item.title,
                                    "payload": item.payload,
                                    "position": position,
                                }
                                for position, item in enumerate(record.activity)
                            ]
                        )
                        .on_conflict_do_nothing(index_elements=["id"])
                    )
            await session.refresh(row)
            return await self._record_from_row(
                session,
                row,
                messages=list(record.messages),
                message_count=len(record.messages),
                activity=list(record.activity),
            )

        return await run_session_operation(self._session_factory, _save)

    async def update_title(self, session_id: str, title: str, title_source: SessionTitleSource = "manual") -> ChatSessionRecord:
        async def _update(session: AsyncSession) -> ChatSessionRecord:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    raise KeyError(session_id)
                row.title = title
                row.title_source = title_source
            await session.refresh(row)
            return await self._record_from_row(session, row)

        return await run_session_operation(self._session_factory, _update)

    async def delete_session(self, session_id: str) -> bool:
        async def _delete(session: AsyncSession) -> bool:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    return False
                await session.delete(row)
                return True

        return await run_session_operation(self._session_factory, _delete)

    @staticmethod
    async def _load_messages(session: AsyncSession, session_id: str) -> list[ChatTranscriptMessage]:
        stmt = (
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.position)
        )
        rows = list(await session.scalars(stmt))
        return [
            ChatTranscriptMessage(
                id=row.id,
                role=row.role,
                content=row.content,
                attachments=list(row.attachments or []),
            )
            for row in rows
        ]

    @staticmethod
    async def _load_activity(session: AsyncSession, session_id: str) -> list[ChatActivityItem]:
        stmt = (
            select(ChatActivityRow)
            .where(ChatActivityRow.session_id == session_id)
            .order_by(ChatActivityRow.position)
        )
        rows = list(await session.scalars(stmt))
        return [
            ChatActivityItem(id=row.id, title=row.title, payload=row.payload)
            for row in rows
        ]

    @staticmethod
    async def _count_messages(session: AsyncSession, session_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
        )
        return int(await session.scalar(stmt) or 0)

    @staticmethod
    def _summary_from_row(row: ChatSessionRow, *, message_count: int) -> ChatSessionSummary:
        return ChatSessionSummary(
            id=row.id,
            title=row.title,
            title_source=row.title_source,
            agent_name=row.agent_name,
            owner_user_id=row.owner_user_id,
            workspace_id=row.workspace_id,
            created_by_token_id=row.created_by_token_id,
            preview_text=row.preview_text,
            message_count=message_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    async def _record_from_row(
        cls,
        session: AsyncSession,
        row: ChatSessionRow,
        *,
        messages: list[ChatTranscriptMessage] | None = None,
        message_count: int | None = None,
        activity: list[ChatActivityItem] | None = None,
    ) -> ChatSessionRecord:
        if messages is None:
            messages = await cls._load_messages(session, row.id)
        if activity is None:
            activity = await cls._load_activity(session, row.id)
        if message_count is None:
            message_count = len(messages)
        return ChatSessionRecord(
            **cls._summary_from_row(row, message_count=message_count).model_dump(),
            memory_messages=[Message.model_validate(item) for item in row.memory_messages_json],
            messages=messages,
            activity=activity,
        )
