from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_000024"
down_revision = "20260727_000023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "activity" not in columns:
        return
    # Copy each activity JSONB entry into a chat_activity row (position = array
    # index) BEFORE dropping the column, atomically with the drop. The app
    # auto-applies migrations to head on startup, so this copy must happen here
    # to avoid data loss. ON CONFLICT (id) DO NOTHING tolerates rows already
    # present (e.g. written after migration #1 by the new store code).
    op.execute(
        """
        INSERT INTO chat_activity (id, session_id, title, payload, position)
        SELECT
            elem->>'id',
            cs.id,
            elem->>'title',
            elem->'payload',
            ordinality - 1
        FROM chat_sessions cs
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(cs.activity, '[]'::jsonb)
        ) WITH ORDINALITY AS t(elem, ordinality)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.drop_column("chat_sessions", "activity")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "activity" in columns:
        return
    op.add_column(
        "chat_sessions",
        sa.Column(
            "activity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
