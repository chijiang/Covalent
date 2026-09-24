"""Add per-provider API style (chat_completions | responses)."""

from alembic import op
import sqlalchemy as sa

revision = "20260921_000033"
down_revision = "20260914_000032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("api_style", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("providers", "api_style")
