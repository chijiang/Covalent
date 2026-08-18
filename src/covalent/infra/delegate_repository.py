"""Delegate run repository — persistence for stateful delegate lifecycles.

Two interchangeable implementations of :class:`DelegateRunStore`:

* :class:`InMemoryDelegateRunStore` — an asyncio.Lock-guarded dict pair for
  tests and lightweight deployments.
* :class:`PostgresDelegateRunStore` — SQLAlchemy access to the
  ``delegate_runs``/``delegate_messages`` tables.

State transitions are optimistic compare-and-set operations on ``version``
plus an allowed source-status list: exactly one racing caller wins and the
losers observe :class:`DelegateRunConflictError` (or
:class:`DelegateRunMissingError` when the run row is gone entirely).
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covalent.core.types import DelegateRunStatus, Message, ParentInputRequest
from covalent.infra.db import DelegateMessageRow, DelegateRunRow

#: Statuses in which a run may still act or still owns resources.
ACTIVE_STATUSES: tuple[DelegateRunStatus, ...] = (
    DelegateRunStatus.CREATED,
    DelegateRunStatus.RUNNING,
    DelegateRunStatus.WAITING_PARENT,
    DelegateRunStatus.IDLE,
)

#: Statuses after which a run row survives as an audit record (with its
#: summary) but its raw messages are pruned once the released-retention
#: window passes.
RETENTION_STATUSES: tuple[DelegateRunStatus, ...] = (
    DelegateRunStatus.RELEASED,
    DelegateRunStatus.FAILED,
    DelegateRunStatus.CANCELLED,
)


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


class DelegateRunConflictError(Exception):
    """CAS failure: version or source-status mismatch on a transition."""


class DelegateRunMissingError(KeyError):
    """Unknown delegate run id."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _record_from_row(row: DelegateRunRow) -> DelegateRunRecord:
    return DelegateRunRecord(
        id=row.id,
        session_id=row.session_id,
        execution_scope_id=row.execution_scope_id,
        workspace_scope_id=row.workspace_scope_id,
        workspace_id=row.workspace_id,
        root_agent_name=row.root_agent_name,
        parent_agent_name=row.parent_agent_name,
        parent_delegate_run_id=row.parent_delegate_run_id,
        delegate_agent_name=row.delegate_agent_name,
        origin_tool_call_id=row.origin_tool_call_id,
        status=DelegateRunStatus(row.status),
        pending_request=(
            ParentInputRequest.model_validate(row.pending_request_json)
            if row.pending_request_json is not None
            else None
        ),
        latest_output=row.latest_output,
        summary=row.summary,
        error=dict(row.error_json),
        release_reason=row.release_reason,
        version=row.version,
        created_at=row.created_at,
        last_activity_at=row.last_activity_at,
        released_at=row.released_at,
        expires_at=row.expires_at,
    )


def _active_values() -> list[str]:
    return [status.value for status in ACTIVE_STATUSES]


def _parent_clause(parent_delegate_run_id: str | None) -> Any:
    if parent_delegate_run_id is None:
        return DelegateRunRow.parent_delegate_run_id.is_(None)
    return DelegateRunRow.parent_delegate_run_id == parent_delegate_run_id


def _transition_updates(
    *,
    expected_version: int,
    status: DelegateRunStatus | None,
    pending_request: ParentInputRequest | None,
    clear_pending: bool,
    latest_output: str | None,
    summary: str | None,
    error: dict[str, Any] | None,
    release_reason: str | None,
    released_at: datetime | None,
    expires_at: datetime | None,
) -> dict[str, Any]:
    """Record-field-shaped CAS updates; an absent key means "leave unchanged"."""
    updates: dict[str, Any] = {"version": expected_version + 1}
    if status is not None:
        updates["status"] = status
    if clear_pending:
        updates["pending_request"] = None
    elif pending_request is not None:
        updates["pending_request"] = pending_request
    if latest_output is not None:
        updates["latest_output"] = latest_output
    if summary is not None:
        updates["summary"] = summary
    if error is not None:
        updates["error"] = dict(error)
    if release_reason is not None:
        updates["release_reason"] = release_reason
    if released_at is not None:
        updates["released_at"] = released_at
    if expires_at is not None:
        updates["expires_at"] = expires_at
    return updates


class DelegateRunStore(ABC):
    @abstractmethod
    async def create_run(self, record: DelegateRunRecord) -> DelegateRunRecord: ...

    @abstractmethod
    async def get_run(self, run_id: str) -> DelegateRunRecord | None: ...

    @abstractmethod
    async def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        from_statuses: tuple[DelegateRunStatus, ...],
        status: DelegateRunStatus | None = None,
        pending_request: ParentInputRequest | None = None,
        clear_pending: bool = False,
        latest_output: str | None = None,
        summary: str | None = None,
        error: dict[str, Any] | None = None,
        release_reason: str | None = None,
        released_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> DelegateRunRecord: ...

    @abstractmethod
    async def list_children(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def count_active_by_parent(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> int: ...

    @abstractmethod
    async def count_by_execution_scope(self, execution_scope_id: str) -> int: ...

    @abstractmethod
    async def save_messages(self, run_id: str, messages: list[Message]) -> None: ...

    @abstractmethod
    async def load_messages(self, run_id: str) -> list[Message]: ...

    @abstractmethod
    async def count_messages(self, run_id: str) -> int: ...

    @abstractmethod
    async def delete_messages(self, run_id: str) -> None: ...

    @abstractmethod
    async def list_stale_running(
        self, *, older_than: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def list_expirable(
        self, *, idle_before: datetime, waiting_before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def list_retention_due(
        self, *, before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def list_active_by_session(self, session_id: str) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def list_active_by_execution_scope(
        self, execution_scope_id: str
    ) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def list_active_for_agent(self, agent_name: str) -> list[DelegateRunRecord]: ...

    @abstractmethod
    async def delete_run(self, run_id: str) -> None: ...

    @abstractmethod
    async def release_scope(
        self, execution_scope_id: str, *, reason: str, released_at: datetime
    ) -> int: ...


class InMemoryDelegateRunStore(DelegateRunStore):
    """Dict-backed store; the lock gives CAS transitions single-winner atomicity."""

    def __init__(self) -> None:
        self._runs: dict[str, DelegateRunRecord] = {}
        self._messages: dict[str, list[Message]] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, record: DelegateRunRecord) -> DelegateRunRecord:
        async with self._lock:
            if record.id in self._runs:
                raise DelegateRunConflictError(f"delegate run {record.id} already exists")
            stored = record.model_copy()  # the store owns its state, like a DB row
            self._runs[record.id] = stored
            return stored

    async def get_run(self, run_id: str) -> DelegateRunRecord | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        from_statuses: tuple[DelegateRunStatus, ...],
        status: DelegateRunStatus | None = None,
        pending_request: ParentInputRequest | None = None,
        clear_pending: bool = False,
        latest_output: str | None = None,
        summary: str | None = None,
        error: dict[str, Any] | None = None,
        release_reason: str | None = None,
        released_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> DelegateRunRecord:
        async with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise DelegateRunMissingError(run_id)
            if record.version != expected_version or record.status not in from_statuses:
                raise DelegateRunConflictError(
                    f"version/status mismatch for {run_id}: "
                    f"expected version={expected_version} from={[s.value for s in from_statuses]}, "
                    f"actual version={record.version} status={record.status.value}"
                )
            updates = _transition_updates(
                expected_version=expected_version,
                status=status,
                pending_request=pending_request,
                clear_pending=clear_pending,
                latest_output=latest_output,
                summary=summary,
                error=error,
                release_reason=release_reason,
                released_at=released_at,
                expires_at=expires_at,
            )
            updates["last_activity_at"] = _utcnow()
            updated = record.model_copy(update=updates)
            self._runs[run_id] = updated
            return updated

    async def list_children(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.execution_scope_id == execution_scope_id
                and record.parent_agent_name == parent_agent_name
                and record.parent_delegate_run_id == parent_delegate_run_id
            ]
            records.sort(key=lambda record: (record.created_at, record.id))
            return records

    async def count_active_by_parent(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> int:
        async with self._lock:
            return sum(
                1
                for record in self._runs.values()
                if record.execution_scope_id == execution_scope_id
                and record.parent_agent_name == parent_agent_name
                and record.parent_delegate_run_id == parent_delegate_run_id
                and record.status in ACTIVE_STATUSES
            )

    async def count_by_execution_scope(self, execution_scope_id: str) -> int:
        async with self._lock:
            return sum(
                1
                for record in self._runs.values()
                if record.execution_scope_id == execution_scope_id
            )

    async def save_messages(self, run_id: str, messages: list[Message]) -> None:
        async with self._lock:
            self._messages[run_id] = list(messages)

    async def load_messages(self, run_id: str) -> list[Message]:
        async with self._lock:
            return list(self._messages.get(run_id, []))

    async def count_messages(self, run_id: str) -> int:
        async with self._lock:
            return len(self._messages.get(run_id, []))

    async def delete_messages(self, run_id: str) -> None:
        async with self._lock:
            self._messages.pop(run_id, None)

    async def list_stale_running(
        self, *, older_than: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.status == DelegateRunStatus.RUNNING
                and record.last_activity_at < older_than
            ]
            records.sort(key=lambda record: (record.last_activity_at, record.id))
            return records[:limit]

    async def list_expirable(
        self, *, idle_before: datetime, waiting_before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._lock:
            records = []
            for record in self._runs.values():
                if record.expires_at is None:
                    continue
                if (
                    record.status == DelegateRunStatus.IDLE
                    and record.expires_at <= idle_before
                ) or (
                    record.status
                    in (DelegateRunStatus.WAITING_PARENT, DelegateRunStatus.CREATED)
                    and record.expires_at <= waiting_before
                ):
                    records.append(record)
            records.sort(key=lambda record: (record.expires_at, record.id))
            return records[:limit]

    async def list_retention_due(
        self, *, before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.status in RETENTION_STATUSES
                and record.released_at is not None
                and record.released_at <= before
            ]
            records.sort(key=lambda record: (record.released_at, record.id))
            return records[:limit]

    async def list_active_by_session(self, session_id: str) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.session_id == session_id and record.status in ACTIVE_STATUSES
            ]
            records.sort(key=lambda record: (record.created_at, record.id))
            return records

    async def list_active_by_execution_scope(
        self, execution_scope_id: str
    ) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.execution_scope_id == execution_scope_id
                and record.status in ACTIVE_STATUSES
            ]
            records.sort(key=lambda record: (record.created_at, record.id))
            return records

    async def list_active_for_agent(self, agent_name: str) -> list[DelegateRunRecord]:
        async with self._lock:
            records = [
                record
                for record in self._runs.values()
                if record.delegate_agent_name == agent_name
                and record.status in ACTIVE_STATUSES
            ]
            records.sort(key=lambda record: (record.created_at, record.id))
            return records

    async def delete_run(self, run_id: str) -> None:
        async with self._lock:
            if run_id not in self._runs:
                return
            # Mirror the FK cascades: children (transitively) and messages go too.
            doomed = {run_id}
            frontier = {run_id}
            while frontier:
                frontier = {
                    record_id
                    for record_id, record in self._runs.items()
                    if record.parent_delegate_run_id in frontier
                }
                doomed |= frontier
            for record_id in doomed:
                self._runs.pop(record_id, None)
                self._messages.pop(record_id, None)

    async def release_scope(
        self, execution_scope_id: str, *, reason: str, released_at: datetime
    ) -> int:
        async with self._lock:
            released = 0
            for run_id, record in self._runs.items():
                if record.execution_scope_id != execution_scope_id:
                    continue
                if record.status not in ACTIVE_STATUSES:
                    continue
                self._runs[run_id] = record.model_copy(
                    update={
                        "status": DelegateRunStatus.RELEASED,
                        "release_reason": reason,
                        "released_at": released_at,
                    }
                )
                released += 1
            return released


class PostgresDelegateRunStore(DelegateRunStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_run(self, record: DelegateRunRecord) -> DelegateRunRecord:
        async with self._session_factory() as session:
            row = DelegateRunRow(
                id=record.id,
                session_id=record.session_id,
                execution_scope_id=record.execution_scope_id,
                workspace_scope_id=record.workspace_scope_id,
                workspace_id=record.workspace_id,
                root_agent_name=record.root_agent_name,
                parent_agent_name=record.parent_agent_name,
                parent_delegate_run_id=record.parent_delegate_run_id,
                delegate_agent_name=record.delegate_agent_name,
                origin_tool_call_id=record.origin_tool_call_id,
                status=record.status.value,
                pending_request_json=(
                    record.pending_request.model_dump(mode="json")
                    if record.pending_request is not None
                    else None
                ),
                latest_output=record.latest_output,
                summary=record.summary,
                error_json=record.error,
                release_reason=record.release_reason,
                version=record.version,
                created_at=record.created_at,
                last_activity_at=record.last_activity_at,
                released_at=record.released_at,
                expires_at=record.expires_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                # Map only duplicate-id (unique violation) to the neutral
                # conflict error; FK/check violations propagate honestly.
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise DelegateRunConflictError(
                        f"delegate run {record.id} already exists"
                    ) from exc
                raise
            await session.refresh(row)
            return _record_from_row(row)

    async def get_run(self, run_id: str) -> DelegateRunRecord | None:
        async with self._session_factory() as session:
            row = await session.get(DelegateRunRow, run_id)
            return _record_from_row(row) if row is not None else None

    async def transition_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        from_statuses: tuple[DelegateRunStatus, ...],
        status: DelegateRunStatus | None = None,
        pending_request: ParentInputRequest | None = None,
        clear_pending: bool = False,
        latest_output: str | None = None,
        summary: str | None = None,
        error: dict[str, Any] | None = None,
        release_reason: str | None = None,
        released_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> DelegateRunRecord:
        updates = _transition_updates(
            expected_version=expected_version,
            status=status,
            pending_request=pending_request,
            clear_pending=clear_pending,
            latest_output=latest_output,
            summary=summary,
            error=error,
            release_reason=release_reason,
            released_at=released_at,
            expires_at=expires_at,
        )
        values: dict[str, Any] = {
            "version": updates["version"],
            "last_activity_at": func.now(),
        }
        if "status" in updates:
            values["status"] = updates["status"].value
        if "pending_request" in updates:
            values["pending_request_json"] = (
                updates["pending_request"].model_dump(mode="json")
                if updates["pending_request"] is not None
                else None
            )
        for column in (
            "latest_output",
            "summary",
            "release_reason",
            "released_at",
            "expires_at",
        ):
            if column in updates:
                values[column] = updates[column]
        if "error" in updates:
            values["error_json"] = updates["error"]
        async with self._session_factory() as session:
            async with session.begin():
                stmt = (
                    update(DelegateRunRow)
                    .where(
                        DelegateRunRow.id == run_id,
                        DelegateRunRow.version == expected_version,
                        DelegateRunRow.status.in_(s.value for s in from_statuses),
                    )
                    .values(**values)
                )
                result = await session.execute(stmt)
                if result.rowcount == 0:
                    row = await session.get(DelegateRunRow, run_id)
                    if row is None:
                        raise DelegateRunMissingError(run_id)
                    raise DelegateRunConflictError(
                        f"version/status mismatch for {run_id}: "
                        f"expected version={expected_version} from={[s.value for s in from_statuses]}, "
                        f"actual version={row.version} status={row.status}"
                    )
                row = await session.get(DelegateRunRow, run_id)
                await session.refresh(row)
                return _record_from_row(row)

    async def list_children(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.execution_scope_id == execution_scope_id,
                        DelegateRunRow.parent_agent_name == parent_agent_name,
                        _parent_clause(parent_delegate_run_id),
                    )
                    .order_by(DelegateRunRow.created_at, DelegateRunRow.id)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def count_active_by_parent(
        self,
        *,
        execution_scope_id: str,
        parent_agent_name: str,
        parent_delegate_run_id: str | None,
    ) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(DelegateRunRow)
                .where(
                    DelegateRunRow.execution_scope_id == execution_scope_id,
                    DelegateRunRow.parent_agent_name == parent_agent_name,
                    _parent_clause(parent_delegate_run_id),
                    DelegateRunRow.status.in_(_active_values()),
                )
            )
            return int(count or 0)

    async def count_by_execution_scope(self, execution_scope_id: str) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(DelegateRunRow)
                .where(DelegateRunRow.execution_scope_id == execution_scope_id)
            )
            return int(count or 0)

    async def save_messages(self, run_id: str, messages: list[Message]) -> None:
        # Delete + reinsert with fresh positions in ONE transaction, so readers
        # never observe a half-replaced transcript.
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(DelegateMessageRow).where(
                        DelegateMessageRow.delegate_run_id == run_id
                    )
                )
                for position, message in enumerate(messages):
                    session.add(
                        DelegateMessageRow(
                            id=uuid.uuid4().hex,
                            delegate_run_id=run_id,
                            role=message.role,
                            content_json=message.model_dump(mode="json"),
                            name=message.name,
                            tool_call_id=message.tool_call_id,
                            tool_calls=list(message.tool_calls),
                            reasoning_content=message.reasoning_content,
                            position=position,
                        )
                    )

    async def load_messages(self, run_id: str) -> list[Message]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateMessageRow)
                    .where(DelegateMessageRow.delegate_run_id == run_id)
                    .order_by(DelegateMessageRow.position)
                )
            )
            return [Message.model_validate(row.content_json) for row in rows]

    async def count_messages(self, run_id: str) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(DelegateMessageRow)
                .where(DelegateMessageRow.delegate_run_id == run_id)
            )
            return int(count or 0)

    async def delete_messages(self, run_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(DelegateMessageRow).where(DelegateMessageRow.delegate_run_id == run_id)
            )
            await session.commit()

    async def list_stale_running(
        self, *, older_than: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.status == DelegateRunStatus.RUNNING.value,
                        DelegateRunRow.last_activity_at < older_than,
                    )
                    .order_by(DelegateRunRow.last_activity_at, DelegateRunRow.id)
                    .limit(limit)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def list_expirable(
        self, *, idle_before: datetime, waiting_before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.expires_at.is_not(None),
                        or_(
                            and_(
                                DelegateRunRow.status == DelegateRunStatus.IDLE.value,
                                DelegateRunRow.expires_at <= idle_before,
                            ),
                            and_(
                                DelegateRunRow.status.in_(
                                    (
                                        DelegateRunStatus.WAITING_PARENT.value,
                                        DelegateRunStatus.CREATED.value,
                                    )
                                ),
                                DelegateRunRow.expires_at <= waiting_before,
                            ),
                        ),
                    )
                    .order_by(DelegateRunRow.expires_at, DelegateRunRow.id)
                    .limit(limit)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def list_retention_due(
        self, *, before: datetime, limit: int = 100
    ) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.status.in_(s.value for s in RETENTION_STATUSES),
                        DelegateRunRow.released_at.is_not(None),
                        DelegateRunRow.released_at <= before,
                    )
                    .order_by(DelegateRunRow.released_at, DelegateRunRow.id)
                    .limit(limit)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def list_active_by_session(self, session_id: str) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.session_id == session_id,
                        DelegateRunRow.status.in_(_active_values()),
                    )
                    .order_by(DelegateRunRow.created_at, DelegateRunRow.id)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def list_active_by_execution_scope(
        self, execution_scope_id: str
    ) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.execution_scope_id == execution_scope_id,
                        DelegateRunRow.status.in_(_active_values()),
                    )
                    .order_by(DelegateRunRow.created_at, DelegateRunRow.id)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def list_active_for_agent(self, agent_name: str) -> list[DelegateRunRecord]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(DelegateRunRow)
                    .where(
                        DelegateRunRow.delegate_agent_name == agent_name,
                        DelegateRunRow.status.in_(_active_values()),
                    )
                    .order_by(DelegateRunRow.created_at, DelegateRunRow.id)
                )
            )
            return [_record_from_row(row) for row in rows]

    async def delete_run(self, run_id: str) -> None:
        # DB-level FK cascades remove child runs and their messages.
        async with self._session_factory() as session:
            await session.execute(delete(DelegateRunRow).where(DelegateRunRow.id == run_id))
            await session.commit()

    async def release_scope(
        self, execution_scope_id: str, *, reason: str, released_at: datetime
    ) -> int:
        async with self._session_factory() as session:
            stmt = (
                update(DelegateRunRow)
                .where(
                    DelegateRunRow.execution_scope_id == execution_scope_id,
                    DelegateRunRow.status.in_(_active_values()),
                )
                .values(
                    status=DelegateRunStatus.RELEASED.value,
                    release_reason=reason,
                    released_at=released_at,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)
