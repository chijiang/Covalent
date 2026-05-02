from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260429_000005"
down_revision = "20260428_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_sources" in inspector.get_table_names():
        return

    op.create_table(
        "skill_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="git"),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="github_synced"),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("subdir", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_sources" in inspector.get_table_names():
        op.drop_table("skill_sources")