"""PostgreSQL persistence for the shared durable run lifecycle."""
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import select, update
from covalent_runtime.services.run_manager import RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING
from covalent_enterprise.infra.db import ChatRunEventRow, ChatRunRow


class PostgresRunStore:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_name: str,
        owner_user_id: str | None,
        workspace_id: str | None,
        input_json: dict[str, Any],
    ) -> int:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ChatRunRow(
                        id=run_id,
                        session_id=session_id,
                        agent_name=agent_name,
                        owner_user_id=owner_user_id,
                        workspace_id=workspace_id,
                        status=RUN_STATUS_RUNNING,
                        input_json=dict(input_json),
                    )
                )
                last = await session.execute(
                    select(ChatRunEventRow.position)
                    .where(ChatRunEventRow.run_id == run_id)
                    .order_by(ChatRunEventRow.position.desc())
                    .limit(1)
                )
                row = last.first()
                return (row[0] + 1) if row is not None else 1

    async def finish_run(self, run_id: str, status: str, error: dict[str, Any] | None = None) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatRunRow)
                    .where(ChatRunRow.id == run_id)
                    .values(
                        status=status,
                        error_json=dict(error or {}),
                        finished_at=now,
                        updated_at=now,
                    )
                )

    async def append_event(self, run_id: str, event_name: str, payload: dict[str, Any], position: int) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ChatRunEventRow(
                        run_id=run_id,
                        position=position,
                        event=event_name,
                        payload=payload,
                    )
                )

    async def get_run(self, run_id: str) -> ChatRunRow | None:
        async with self._session_factory() as session:
            result = await session.execute(select(ChatRunRow).where(ChatRunRow.id == run_id))
            return result.scalar_one_or_none()

    async def open_run_for_session(self, session_id: str) -> ChatRunRow | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow)
                .where(
                    ChatRunRow.session_id == session_id,
                    ChatRunRow.status.in_([RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING]),
                )
                .order_by(ChatRunRow.created_at.desc())
            )
            return result.scalars().first()

    async def list_runs_for_session(self, session_id: str) -> list[ChatRunRow]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow)
                .where(ChatRunRow.session_id == session_id)
                .order_by(ChatRunRow.created_at.asc())
            )
            return list(result.scalars().all())

    async def mark_cancelling(self, run_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatRunRow)
                    .where(ChatRunRow.id == run_id, ChatRunRow.status == RUN_STATUS_RUNNING)
                    .values(status=RUN_STATUS_CANCELLING)
                )

    async def events_after(self, run_id: str, after_position: int) -> list[tuple[int, str, dict[str, Any]]]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunEventRow)
                .where(ChatRunEventRow.run_id == run_id, ChatRunEventRow.position > after_position)
                .order_by(ChatRunEventRow.position.asc())
            )
            return [(row.position, row.event, row.payload) for row in result.scalars()]

    async def stale_run_ids(self) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChatRunRow.id).where(
                    ChatRunRow.status.in_([RUN_STATUS_RUNNING, RUN_STATUS_CANCELLING])
                )
            )
            return [row[0] for row in result.all()]
