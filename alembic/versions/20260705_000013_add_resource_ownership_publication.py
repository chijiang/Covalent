from __future__ import annotations

"""add resource ownership and publication workflow"""

from alembic import op
import sqlalchemy as sa


revision = "20260705_000013"
down_revision = "20260705_000012"
branch_labels = None
depends_on = None


RESOURCE_TABLES = ("agents", "mcp_servers", "skill_sources", "providers")
NAMED_RESOURCE_TABLES = ("agents", "mcp_servers", "providers")


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns(table_name)}
    if column_name in columns:
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name in RESOURCE_TABLES:
        if table_name not in tables:
            continue
        if table_name in NAMED_RESOURCE_TABLES:
            _add_column_if_missing(table_name, sa.Column("display_name", sa.String(length=255), nullable=True))
        _add_column_if_missing(table_name, sa.Column("owner_user_id", sa.String(length=255), nullable=True))
        _add_column_if_missing(table_name, sa.Column("workspace_id", sa.String(length=255), nullable=True))
        _add_column_if_missing(table_name, sa.Column("visibility", sa.String(length=32), nullable=False, server_default="public"))
        _add_column_if_missing(table_name, sa.Column("publication_status", sa.String(length=32), nullable=False, server_default="approved"))
        _add_column_if_missing(table_name, sa.Column("publication_requested_at", sa.DateTime(timezone=True), nullable=True))
        _add_column_if_missing(table_name, sa.Column("publication_reviewed_at", sa.DateTime(timezone=True), nullable=True))
        _add_column_if_missing(table_name, sa.Column("publication_reviewed_by_user_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name in RESOURCE_TABLES:
        if table_name not in tables:
            continue
        for column_name in (
            "publication_reviewed_by_user_id",
            "publication_reviewed_at",
            "publication_requested_at",
            "publication_status",
            "visibility",
            "workspace_id",
            "owner_user_id",
            "display_name",
        ):
            _drop_column_if_present(table_name, column_name)
