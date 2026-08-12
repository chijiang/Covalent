"""MCP server repository — data access for ``mcp_servers`` + env vars.

Extracted from ``ConfigStore`` so the store keeps aggregation/visibility logic
while this class owns the raw table CRUD.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from covalent.infra.db import McpServerEnvVarRow, McpServerRow
from covalent.mcp.spec import McpServerConfig


class McpRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_servers(self, principal) -> list[dict[str, object]]:
        from covalent.infra.config_store import _public_mcp_config, _resource_metadata_from_row, _resource_scope_clause
        async with self._session_factory() as session:
            server_rows = list(
                await session.scalars(
                    select(McpServerRow)
                    .where(_resource_scope_clause(McpServerRow, principal))
                    .order_by(McpServerRow.position, McpServerRow.name)
                )
            )
            env_rows = list(
                await session.scalars(
                    select(McpServerEnvVarRow).order_by(McpServerEnvVarRow.server_name, McpServerEnvVarRow.key)
                )
            )

        env_map: dict[str, dict[str, str]] = defaultdict(dict)
        for row in env_rows:
            env_map[row.server_name][row.key] = row.value

        payload: list[dict[str, object]] = []
        for row in server_rows:
            item = _public_mcp_config(row, env_map.get(row.name, {})).model_dump(mode="json")
            item.update(_resource_metadata_from_row(row))
            payload.append(item)
        return payload

    async def save_servers(self, payload: list[dict[str, object]], principal) -> None:
        from covalent.infra.config_store import (
            PersistedMcpServerMetadata,
            _apply_resource_metadata,
            _find_owned_row_by_public_name,
            _is_editable_by_principal,
            _owned_resource_clause,
            _scoped_resource_name,
        )
        parsed_items = [
            (McpServerConfig.model_validate(item), PersistedMcpServerMetadata.model_validate(item))
            for item in payload
        ]
        parsed_items = [
            (server, metadata)
            for server, metadata in parsed_items
            if _is_editable_by_principal(metadata, principal)
        ]
        servers = [server for server, _metadata in parsed_items]
        metadata_items = [metadata for _server, metadata in parsed_items]
        async with self._session_factory() as session:
            async with session.begin():
                existing_rows = {
                    row.name: row
                    for row in list(await session.scalars(select(McpServerRow).where(_owned_resource_clause(McpServerRow, principal))))
                }
                resolved_names: list[str] = []
                for server, metadata in parsed_items:
                    resolved_names.append(metadata.internal_name or _scoped_resource_name(server.name, principal))
                incoming_names = set(resolved_names)

                for name, row in existing_rows.items():
                    if name not in incoming_names:
                        await session.delete(row)

                for position, server in enumerate(servers):
                    public_name = server.name
                    internal_name = resolved_names[position]
                    row = existing_rows.get(internal_name) or _find_owned_row_by_public_name(list(existing_rows.values()), public_name)
                    if row is None:
                        row = McpServerRow(name=internal_name)
                        session.add(row)
                    row.display_name = public_name if public_name != row.name else None
                    existing_visibility = row.visibility
                    existing_status = row.publication_status
                    row.position = position
                    row.transport = server.transport
                    row.command = server.command
                    row.args = list(server.args)
                    row.url = server.url
                    _apply_resource_metadata(
                        row,
                        metadata_items[position],
                        principal,
                        existing_visibility=existing_visibility,
                        existing_status=existing_status,
                    )

                    await session.execute(delete(McpServerEnvVarRow).where(McpServerEnvVarRow.server_name == row.name))
                    for key, value in sorted((server.env or {}).items()):
                        session.add(McpServerEnvVarRow(server_name=row.name, key=key, value=value))
