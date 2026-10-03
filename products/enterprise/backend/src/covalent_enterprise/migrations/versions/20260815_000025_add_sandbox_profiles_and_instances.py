from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260815_000025"
down_revision = "20260727_000024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sandbox_profiles" in inspector.get_table_names():
        return

    op.create_table(
        "sandbox_profiles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("pull_policy", sa.String(length=32), nullable=False, server_default="if_not_present"),
        sa.Column("keepalive_command", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("runtime_capabilities", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("contract_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("memory_limit", sa.String(length=32), nullable=False),
        sa.Column("pids_limit", sa.Integer(), nullable=False),
        sa.Column("cpus", sa.Float(), nullable=False),
        sa.Column("tmpfs_size", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("validated_image_id", sa.Text(), nullable=True),
        sa.Column("validated_image_digest", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("validation_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_sandbox_profiles_workspace_name"),
        sa.PrimaryKeyConstraint("id"),
    )

    agents_columns = {column["name"] for column in inspector.get_columns("agents")}
    if "sandbox_profile_id" not in agents_columns:
        op.add_column(
            "agents",
            sa.Column(
                "sandbox_profile_id",
                sa.String(length=64),
                sa.ForeignKey("sandbox_profiles.id", ondelete="RESTRICT", name="fk_agents_sandbox_profile_id_sandbox_profiles"),
                nullable=True,
            ),
        )

    op.create_table(
        "sandbox_instances",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("execution_scope_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("profile_name_snapshot", sa.String(length=255), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("spec_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_outbound_snapshot", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            ondelete="CASCADE",
            name="fk_sandbox_instance_session",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["sandbox_profiles.id"],
            ondelete="RESTRICT",
            name="fk_sandbox_instance_profile",
        ),
        sa.UniqueConstraint("execution_scope_id", "agent_name", name="uq_sandbox_instances_scope_agent"),
        sa.CheckConstraint("scope_kind IN ('session', 'run')", name="ck_sandbox_instances_scope_kind"),
        sa.CheckConstraint(
            "(scope_kind = 'session' AND session_id IS NOT NULL) OR (scope_kind = 'run' AND session_id IS NULL)",
            name="ck_sandbox_instances_scope_session",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sandbox_instances_session_id", "sandbox_instances", ["session_id"])
    op.create_index("ix_sandbox_instances_execution_scope_id", "sandbox_instances", ["execution_scope_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sandbox_instances" in inspector.get_table_names():
        op.drop_index("ix_sandbox_instances_execution_scope_id", table_name="sandbox_instances")
        op.drop_index("ix_sandbox_instances_session_id", table_name="sandbox_instances")
        op.drop_table("sandbox_instances")
    agents_columns = {column["name"] for column in inspector.get_columns("agents")}
    if "sandbox_profile_id" in agents_columns:
        op.drop_column("agents", "sandbox_profile_id")
    if "sandbox_profiles" in inspector.get_table_names():
        op.drop_table("sandbox_profiles")
