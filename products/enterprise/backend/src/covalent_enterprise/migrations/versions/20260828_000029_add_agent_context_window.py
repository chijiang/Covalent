"""add agent context_window column"""

from alembic import op
import sqlalchemy as sa


revision = "20260828_000029"
down_revision = "20260826_000028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "context_window" not in columns:
        op.add_column("agents", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "context_window" in columns:
        op.drop_column("agents", "context_window")
