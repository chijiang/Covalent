from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# upgrade() copies transcript_messages into chat_messages BEFORE dropping the column (atomic, data-safe).
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
    # Copy each transcript_messages JSONB entry into a chat_messages row
    # (position = array index) BEFORE dropping the column, so conversation
    # history survives the cutover. The app auto-applies migrations to head
    # on startup (api/app.py lifespan -> run_database_migrations -> upgrade
    # head), so this copy must happen here, atomically with the drop, to
    # avoid data loss. ON CONFLICT (id) DO NOTHING keeps it idempotent if
    # chat_messages already holds rows (e.g. a prior manual backfill).
    op.execute(
        """
        INSERT INTO chat_messages (id, session_id, role, content, attachments, position)
        SELECT
            elem->>'id',
            cs.id,
            elem->>'role',
            elem->>'content',
            COALESCE(elem->'attachments', '[]'::jsonb),
            ordinality - 1
        FROM chat_sessions cs
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(cs.transcript_messages, '[]'::jsonb)
        ) WITH ORDINALITY AS t(elem, ordinality)
        ON CONFLICT (id) DO NOTHING
        """
    )
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
