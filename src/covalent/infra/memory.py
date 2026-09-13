from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covalent.core.types import Message
from covalent.infra.db import ChatActivityRow, ChatMessageRow, ChatSessionRow, run_session_operation


SessionTitleSource = Literal["auto", "manual"]


class ChatTranscriptMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    reasoning_content: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # Stable transcript ordinal (0-based). None when the message was built
    # in-memory and not yet assigned a row position.
    position: int | None = None


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
    async def get_session(
        self,
        session_id: str,
        *,
        messages_limit: int | None = None,
        messages_before_position: int | None = None,
    ) -> ChatSessionRecord | None:
        """Load a session record.

        With ``messages_limit`` / ``messages_before_position`` the transcript
        is a newest-first page (positions strictly below
        ``messages_before_position`` when given); ``record.message_count``
        still reflects the session's total message count so callers can
        detect has-more. Default (both None) loads the full transcript.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_session_summary(self, session_id: str) -> ChatSessionSummary | None:
        """Lightweight session lookup (no messages/activity) for auth checks."""
        raise NotImplementedError

    @abstractmethod
    async def get_activity_item(self, session_id: str, activity_id: str) -> ChatActivityItem | None:
        raise NotImplementedError

    @abstractmethod
    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        """Persist the session.

        ``record.activity`` must be a superset of the currently-stored
        activity items (append-only): existing items are kept by id and only
        net-new items are added. Callers must NOT drop or reorder items, and
        must NOT mutate an already-stored item's id — on the persistent store
        a non-superset can raise ``IntegrityError`` on the
        ``(session_id, position)`` unique constraint.
        """
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

    async def get_session(
        self,
        session_id: str,
        *,
        messages_limit: int | None = None,
        messages_before_position: int | None = None,
    ) -> ChatSessionRecord | None:
        record = self._sessions.get(session_id)
        if record is None:
            return None
        if messages_limit is None and messages_before_position is None:
            full = record.model_copy(deep=True)
            if any(message.position is None for message in full.messages):
                full.messages = [
                    message.model_copy(deep=True, update={"position": position})
                    for position, message in enumerate(full.messages)
                ]
            return full
        messages = record.messages
        start = 0
        if messages_before_position is not None:
            messages = messages[:messages_before_position]
        if messages_limit is not None:
            tail = messages[-messages_limit:] if messages_limit > 0 else []
            start = len(messages) - len(tail)
            messages = tail
        paged = record.model_copy(deep=True)
        paged.messages = [
            message.model_copy(deep=True, update={"position": start + offset})
            for offset, message in enumerate(messages)
        ]
        paged.message_count = len(record.messages)
        return paged

    async def get_session_summary(self, session_id: str) -> ChatSessionSummary | None:
        record = self._sessions.get(session_id)
        if record is None:
            return None
        return ChatSessionSummary.model_validate(record.model_dump())

    async def get_activity_item(self, session_id: str, activity_id: str) -> ChatActivityItem | None:
        record = self._sessions.get(session_id)
        if record is None:
            return None
        for item in record.activity:
            if item.id == activity_id:
                return item.model_copy(deep=True)
        return None

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

    async def get_session(
        self,
        session_id: str,
        *,
        messages_limit: int | None = None,
        messages_before_position: int | None = None,
    ) -> ChatSessionRecord | None:
        async def _get(session: AsyncSession) -> ChatSessionRecord | None:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return None
            if messages_limit is None and messages_before_position is None:
                return await self._record_from_row(session, row)
            messages, total = await self._load_message_page(
                session,
                session_id,
                limit=messages_limit,
                before_position=messages_before_position,
            )
            return await self._record_from_row(session, row, messages=messages, message_count=total)

        return await run_session_operation(self._session_factory, _get)

    async def get_session_summary(self, session_id: str) -> ChatSessionSummary | None:
        async def _get(session: AsyncSession) -> ChatSessionSummary | None:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return None
            count = await self._count_messages(session, session_id)
            return self._summary_from_row(row, message_count=count)

        return await run_session_operation(self._session_factory, _get)

    async def get_activity_item(self, session_id: str, activity_id: str) -> ChatActivityItem | None:
        async def _get(session: AsyncSession) -> ChatActivityItem | None:
            stmt = select(ChatActivityRow).where(
                ChatActivityRow.session_id == session_id,
                ChatActivityRow.id == activity_id,
            )
            row = await session.scalar(stmt)
            if row is None:
                return None
            return ChatActivityItem(id=row.id, title=row.title, payload=row.payload)

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
                            reasoning_content=message.reasoning_content,
                            attachments=list(message.attachments),
                            position=position,
                        )
                    )
                # Append-only: callers must pass a superset (see SessionStore.save_session).
                # Existing ids are no-ops via ON CONFLICT; only net-new rows insert.
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
                reasoning_content=row.reasoning_content or "",
                attachments=list(row.attachments or []),
                position=row.position,
            )
            for row in rows
        ]

    @staticmethod
    async def _load_message_page(
        session: AsyncSession,
        session_id: str,
        *,
        limit: int | None,
        before_position: int | None,
    ) -> tuple[list[ChatTranscriptMessage], int]:
        """Newest-first window over the transcript, returned oldest-first.

        Selects the ``limit`` messages with the highest position strictly below
        ``before_position`` (all messages when None), plus the session's total
        message count so callers can detect has-more.
        """
        stmt = select(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        if before_position is not None:
            stmt = stmt.where(ChatMessageRow.position < before_position)
        stmt = stmt.order_by(desc(ChatMessageRow.position))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list(await session.scalars(stmt))
        total = await PersistentSessionStore._count_messages(session, session_id)
        rows.reverse()
        messages = [
            ChatTranscriptMessage(
                id=row.id,
                role=row.role,
                content=row.content,
                reasoning_content=row.reasoning_content or "",
                attachments=list(row.attachments or []),
                position=row.position,
            )
            for row in rows
        ]
        return messages, total

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
