"""add agent enabled flag"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_000020"
down_revision = "20260720_000019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "enabled" not in columns:
        op.add_column(
            "agents",
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.alter_column("agents", "enabled", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "enabled" in columns:
        op.drop_column("agents", "enabled")
