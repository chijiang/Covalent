from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_000023"
down_revision = "20260724_000022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_activity" in inspector.get_table_names():
        return

    op.create_table(
        "chat_activity",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            ondelete="CASCADE",
            name="fk_chat_activity_session_id_chat_sessions",
        ),
        sa.UniqueConstraint("session_id", "position", name="uq_chat_activity_session_id_position"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_activity_session_id", "chat_activity", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_activity" not in inspector.get_table_names():
        return
    op.drop_index("ix_chat_activity_session_id", table_name="chat_activity")
    op.drop_table("chat_activity")
