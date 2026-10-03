from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260501_000008"
down_revision = "20260430_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_states" in inspector.get_table_names():
        return

    op.create_table(
        "skill_states",
        sa.Column("skill_name", sa.String(length=255), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_states" in inspector.get_table_names():
        op.drop_table("skill_states")