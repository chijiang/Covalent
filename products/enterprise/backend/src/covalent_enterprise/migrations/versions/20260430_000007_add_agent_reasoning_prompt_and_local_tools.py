from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260430_000007"
down_revision = "20260430_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "reasoning_prompt" not in columns:
        op.add_column("agents", sa.Column("reasoning_prompt", sa.Text(), nullable=False, server_default=""))
    if "local_tools" not in columns:
        op.add_column(
            "agents",
            sa.Column(
                "local_tools",
                postgresql.ARRAY(sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::text[]"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "local_tools" in columns:
        op.drop_column("agents", "local_tools")
    if "reasoning_prompt" in columns:
        op.drop_column("agents", "reasoning_prompt")