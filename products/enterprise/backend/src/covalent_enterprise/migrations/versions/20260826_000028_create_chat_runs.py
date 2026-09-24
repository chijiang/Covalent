from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260826_000028"
down_revision = "20260818_000027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_runs"):
        op.create_table(
            "chat_runs",
            sa.Column("id", sa.String(255), primary_key=True),
            sa.Column("session_id", sa.String(255), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_name", sa.String(255), nullable=False),
            sa.Column("owner_user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("workspace_id", sa.String(255), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.CheckConstraint(
                "status IN ('running','cancelling','completed','cancelled','failed')",
                name="ck_chat_runs_status",
            ),
            sa.Column("input", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("error", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_chat_runs_session_id", "chat_runs", ["session_id"])
        op.create_index("ix_chat_runs_session_status", "chat_runs", ["session_id", "status"])
    if not inspector.has_table("chat_run_events"):
        op.create_table(
            "chat_run_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(255), sa.ForeignKey("chat_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("event", sa.String(64), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("run_id", "position", name="uq_chat_run_events_run_position"),
        )
        op.create_index("ix_chat_run_events_run_position", "chat_run_events", ["run_id", "position"])


def downgrade() -> None:
    op.drop_table("chat_run_events")
    op.drop_table("chat_runs")
