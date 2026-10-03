"""Add APIH provider connection configuration."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_000030"
down_revision = "20260828_000029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("providers", sa.Column("apih_config", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("providers", "apih_config")
