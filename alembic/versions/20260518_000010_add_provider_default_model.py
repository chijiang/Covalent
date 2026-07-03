from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260518_000010"
down_revision = "20260517_000009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "providers" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("providers")}
    if "default_model" not in columns:
        op.add_column(
            "providers",
            sa.Column("default_model", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "providers" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("providers")}
    if "default_model" in columns:
        op.drop_column("providers", "default_model")
