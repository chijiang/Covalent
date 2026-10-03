from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260428_000004"
down_revision = "20260428_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_sources" in inspector.get_table_names():
        op.drop_table("skill_sources")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_sources" not in inspector.get_table_names():
        op.create_table(
            "skill_sources",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_type", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("ref", sa.String(length=255), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )