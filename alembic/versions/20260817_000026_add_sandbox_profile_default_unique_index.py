from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260817_000026"
down_revision = "20260815_000025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {
        index["name"] for index in inspector.get_indexes("sandbox_profiles")
    }
    if "uq_sandbox_profiles_workspace_default" in existing:
        return
    # Database-level guard for "at most one enabled default per workspace" for
    # workspace-scoped profiles. Global defaults (workspace_id IS NULL) are not
    # deduplicated by unique indexes (NULL != NULL) and stay enforced by the
    # application service's atomic set_default_profile transaction.
    op.create_index(
        "uq_sandbox_profiles_workspace_default",
        "sandbox_profiles",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND workspace_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {
        index["name"] for index in inspector.get_indexes("sandbox_profiles")
    }
    if "uq_sandbox_profiles_workspace_default" not in existing:
        return
    op.drop_index("uq_sandbox_profiles_workspace_default", table_name="sandbox_profiles")
