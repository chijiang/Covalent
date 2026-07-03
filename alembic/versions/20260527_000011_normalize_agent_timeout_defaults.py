from __future__ import annotations

"""normalize agent timeout defaults"""

from alembic import op
import sqlalchemy as sa


revision = "20260527_000011"
down_revision = "ddbbce47fb95"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "provider_timeout_seconds" not in columns:
        return

    op.alter_column("agents", "provider_timeout_seconds", server_default="500")
    op.execute(
        sa.text(
            "UPDATE agents "
            "SET provider_timeout_seconds = 500 "
            "WHERE provider_timeout_seconds = 30"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "provider_timeout_seconds" not in columns:
        return

    op.execute(
        sa.text(
            "UPDATE agents "
            "SET provider_timeout_seconds = 30 "
            "WHERE provider_timeout_seconds = 500"
        )
    )
    op.alter_column("agents", "provider_timeout_seconds", server_default="30")
