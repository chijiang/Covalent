from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260428_000003"
down_revision = "20260428_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_mcp_tools",
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_name"], ["mcp_servers.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_name", "server_name", "tool_name"),
    )


def downgrade() -> None:
    op.drop_table("agent_mcp_tools")