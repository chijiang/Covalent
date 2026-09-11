"""Persist assistant reasoning content on chat transcript messages."""

from alembic import op
import sqlalchemy as sa

revision = "20260911_000031"
down_revision = "20260906_000030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("reasoning_content", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "reasoning_content")
