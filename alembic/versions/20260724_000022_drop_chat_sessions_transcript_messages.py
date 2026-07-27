from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260724_000022"
down_revision = "20260724_000021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "transcript_messages" not in columns:
        return
    op.drop_column("chat_sessions", "transcript_messages")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "transcript_messages" in columns:
        return
    op.add_column(
        "chat_sessions",
        sa.Column(
            "transcript_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
