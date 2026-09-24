from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260818_000027"
down_revision = "20260817_000026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("delegate_runs"):
        return
    op.create_table(
        "delegate_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("session_id", sa.String(255), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=True),
        sa.Column("execution_scope_id", sa.String(255), nullable=False),
        sa.Column("workspace_scope_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=True),
        sa.Column("root_agent_name", sa.String(255), nullable=False),
        sa.Column("parent_agent_name", sa.String(255), nullable=False),
        sa.Column("parent_delegate_run_id", sa.String(96), sa.ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("delegate_agent_name", sa.String(255), nullable=False),
        sa.Column("origin_tool_call_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.CheckConstraint("status IN ('created','running','waiting_parent','idle','released','cancelled','failed','expired')", name="ck_delegate_runs_status"),
        sa.Column("pending_request_json", postgresql.JSONB(), nullable=True),
        sa.Column("latest_output", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("release_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_delegate_runs_session_id", "delegate_runs", ["session_id"])
    op.create_index("ix_delegate_runs_execution_scope_id", "delegate_runs", ["execution_scope_id"])
    op.create_index("ix_delegate_runs_parent", "delegate_runs", ["parent_delegate_run_id", "parent_agent_name", "status"])
    op.create_table(
        "delegate_messages",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("delegate_run_id", sa.String(96), sa.ForeignKey("delegate_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content_json", postgresql.JSONB(), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("tool_call_id", sa.String(255), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("reasoning_content", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("delegate_run_id", "position", name="uq_delegate_messages_run_position"),
    )
    op.create_index("ix_delegate_messages_run", "delegate_messages", ["delegate_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("delegate_messages"):
        return
    op.drop_index("ix_delegate_messages_run", table_name="delegate_messages")
    op.drop_table("delegate_messages")
    op.drop_index("ix_delegate_runs_parent", table_name="delegate_runs")
    op.drop_index("ix_delegate_runs_execution_scope_id", table_name="delegate_runs")
    op.drop_index("ix_delegate_runs_session_id", table_name="delegate_runs")
    op.drop_table("delegate_runs")
