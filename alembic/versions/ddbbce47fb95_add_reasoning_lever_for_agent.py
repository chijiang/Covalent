from __future__ import annotations

"""add reasoning lever for agent"""

from alembic import op
import sqlalchemy as sa


revision = 'ddbbce47fb95'
down_revision = '20260518_000010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "reasoning_level" not in columns:
        op.add_column(
            "agents",
            sa.Column("reasoning_level", sa.String(length=32), nullable=False, server_default="none"),
        )
        op.alter_column("agents", "reasoning_level", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "reasoning_level" in columns:
        op.drop_column("agents", "reasoning_level")
