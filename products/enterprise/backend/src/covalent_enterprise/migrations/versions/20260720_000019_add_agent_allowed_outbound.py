"""add agent allowed_outbound for egress whitelist"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_000019"
down_revision = "20260716_000018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "allowed_outbound" not in columns:
        op.add_column(
            "agents",
            sa.Column(
                "allowed_outbound",
                sa.ARRAY(sa.Text),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        op.alter_column("agents", "allowed_outbound", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "agents" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("agents")}
    if "allowed_outbound" in columns:
        op.drop_column("agents", "allowed_outbound")
