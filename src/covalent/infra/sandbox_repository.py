"""Sandbox repository — data access for ``sandbox_profiles`` + ``sandbox_instances``.

Profiles are administrator-managed environment templates; instances are logical
(per execution scope, per agent) bindings that snapshot the profile revision
they were created from. Both are plain row access here — profile validation,
default uniqueness, and agent compatibility live in the application services.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covalent.infra.db import AgentRow, ChatSessionRow, SandboxInstanceRow, SandboxProfileRow

_PROFILE_COLUMNS = (
    "id",
    "name",
    "description",
    "workspace_id",
    "image",
    "pull_policy",
    "keepalive_command",
    "runtime_capabilities",
    "contract_version",
    "memory_limit",
    "pids_limit",
    "cpus",
    "tmpfs_size",
    "enabled",
    "is_default",
    "revision",
    "validation_status",
    "validated_image_id",
    "validated_image_digest",
    "validated_at",
    "validation_message",
    "created_at",
)

_BINDING_COLUMNS = (
    "id",
    "execution_scope_id",
    "session_id",
    "scope_kind",
    "agent_name",
    "profile_id",
    "profile_name_snapshot",
    "profile_revision",
    "spec_snapshot",
    "allowed_outbound_snapshot",
    "created_at",
    "last_used_at",
)


def _profile_dict(row: SandboxProfileRow) -> dict[str, Any]:
    return {column: getattr(row, column) for column in _PROFILE_COLUMNS}


def _binding_dict(row: SandboxInstanceRow) -> dict[str, Any]:
    return {column: getattr(row, column) for column in _BINDING_COLUMNS}


class SandboxRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # --- profiles ---------------------------------------------------------

    async def list_profiles(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = list(await session.scalars(select(SandboxProfileRow).order_by(SandboxProfileRow.created_at, SandboxProfileRow.id)))
            return [_profile_dict(row) for row in rows]

    async def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(SandboxProfileRow, profile_id)
            return _profile_dict(row) if row is not None else None

    async def create_profile(self, values: dict[str, Any]) -> dict[str, Any]:
        async with self._session_factory() as session:
            row = SandboxProfileRow(**{key: values[key] for key in _PROFILE_COLUMNS if key != "created_at" and key in values})
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _profile_dict(row)

    async def update_profile(self, profile_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(SandboxProfileRow, profile_id)
            if row is None:
                return None
            for key, value in values.items():
                if key != "created_at":
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return _profile_dict(row)

    async def delete_profile(self, profile_id: str) -> bool:
        """Delete an unreferenced profile. Raises ``IntegrityError`` while an
        agent or sandbox instance still references it (FK RESTRICT)."""
        async with self._session_factory() as session:
            row = await session.get(SandboxProfileRow, profile_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def count_profile_references(self, profile_id: str) -> dict[str, int]:
        async with self._session_factory() as session:
            agent_count = (
                await session.execute(
                    select(func.count()).select_from(AgentRow).where(AgentRow.sandbox_profile_id == profile_id)
                )
            ).scalar_one()
            instance_count = (
                await session.execute(
                    select(func.count())
                    .select_from(SandboxInstanceRow)
                    .where(SandboxInstanceRow.profile_id == profile_id)
                )
            ).scalar_one()
            return {"agents": int(agent_count), "instances": int(instance_count)}

    async def set_default_profile(self, profile_id: str, workspace_id: str | None) -> bool:
        """Promote one profile to default and demote the previous default of the
        same availability scope in ONE transaction — a failure or concurrent
        switch can never leave zero or multiple defaults."""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(SandboxProfileRow, profile_id)
                if row is None:
                    return False
                scope_clause = (
                    SandboxProfileRow.workspace_id.is_(None)
                    if workspace_id is None
                    else SandboxProfileRow.workspace_id == workspace_id
                )
                await session.execute(
                    update(SandboxProfileRow)
                    .where(
                        SandboxProfileRow.is_default.is_(True),
                        scope_clause,
                        SandboxProfileRow.id != profile_id,
                    )
                    .values(is_default=False)
                )
                row.is_default = True
                return True

    # --- bindings ---------------------------------------------------------

    async def ensure_session_row(self, session_id: str, agent_name: str | None = None) -> None:
        """Create a bare chat_sessions row if absent, atomically.

        Console run/stream starts the runtime (and therefore session-scoped
        sandbox binding creation) before the route persists the real session
        row in its ``finally`` — the binding FK needs the row to exist first.
        The later save upserts full transcript/ownership onto this bare row.
        """
        statement = (
            pg_insert(ChatSessionRow)
            .values(id=session_id, agent_name=agent_name)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def get_binding(self, execution_scope_id: str, agent_name: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SandboxInstanceRow)
                .where(SandboxInstanceRow.execution_scope_id == execution_scope_id)
                .where(SandboxInstanceRow.agent_name == agent_name)
            )
            return _binding_dict(row) if row is not None else None

    async def create_binding(self, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a logical binding. Raises ``IntegrityError`` on a concurrent
        insert for the same ``(execution_scope_id, agent_name)`` — callers
        should roll back and load the winner (see the binding service)."""
        async with self._session_factory() as session:
            row = SandboxInstanceRow(**{key: values[key] for key in _BINDING_COLUMNS if key not in ("created_at", "last_used_at") and key in values})
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _binding_dict(row)

    async def update_binding_outbound(self, instance_id: str, allowed_outbound: list[str]) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(SandboxInstanceRow, instance_id)
            if row is None:
                return None
            row.allowed_outbound_snapshot = list(allowed_outbound)
            await session.commit()
            await session.refresh(row)
            return _binding_dict(row)

    async def list_bindings_by_scope(self, execution_scope_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SandboxInstanceRow)
                    .where(SandboxInstanceRow.execution_scope_id == execution_scope_id)
                    .order_by(SandboxInstanceRow.created_at, SandboxInstanceRow.id)
                )
            )
            return [_binding_dict(row) for row in rows]

    async def list_bindings_by_session(self, session_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SandboxInstanceRow)
                    .where(SandboxInstanceRow.session_id == session_id)
                    .order_by(SandboxInstanceRow.created_at, SandboxInstanceRow.id)
                )
            )
            return [_binding_dict(row) for row in rows]

    async def list_bindings_by_profile(self, profile_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SandboxInstanceRow)
                    .where(SandboxInstanceRow.profile_id == profile_id)
                    .order_by(SandboxInstanceRow.created_at, SandboxInstanceRow.id)
                )
            )
            return [_binding_dict(row) for row in rows]

    async def list_all_bindings(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = list(
                await session.scalars(
                    select(SandboxInstanceRow).order_by(SandboxInstanceRow.created_at, SandboxInstanceRow.id)
                )
            )
            return [_binding_dict(row) for row in rows]

    async def delete_binding(self, instance_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(SandboxInstanceRow, instance_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_bindings_by_scope(self, execution_scope_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SandboxInstanceRow).where(SandboxInstanceRow.execution_scope_id == execution_scope_id)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def touch_binding(self, instance_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(SandboxInstanceRow, instance_id)
            if row is None:
                return False
            row.last_used_at = func.now()
            await session.commit()
            return True
