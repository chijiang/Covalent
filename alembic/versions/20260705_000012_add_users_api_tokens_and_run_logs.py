from __future__ import annotations

"""add users api tokens and run logs"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260705_000012"
down_revision = "20260527_000011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("auth_subject", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email"),
            sa.UniqueConstraint("auth_subject"),
        )

    if "workspaces" not in tables:
        op.create_table(
            "workspaces",
            sa.Column("id", sa.String(length=255), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("slug"),
        )

    if "workspace_members" not in tables:
        op.create_table(
            "workspace_members",
            sa.Column("workspace_id", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        )

    if "api_tokens" not in tables:
        op.create_table(
            "api_tokens",
            sa.Column("id", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=False),
            sa.Column("workspace_id", sa.String(length=255), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("token_prefix", sa.String(length=32), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
            sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_prefix"),
        )

    if "agent_access_grants" not in tables:
        op.create_table(
            "agent_access_grants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workspace_id", sa.String(length=255), nullable=False),
            sa.Column("agent_name", sa.String(length=255), nullable=False),
            sa.Column("subject_type", sa.String(length=32), nullable=False),
            sa.Column("subject_id", sa.String(length=255), nullable=False),
            sa.Column("permissions", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["agent_name"], ["agents.name"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "agent_name", "subject_type", "subject_id", name="uq_agent_access_grant_subject"),
        )

    if "agent_run_logs" not in tables:
        op.create_table(
            "agent_run_logs",
            sa.Column("id", sa.String(length=255), nullable=False),
            sa.Column("user_id", sa.String(length=255), nullable=True),
            sa.Column("token_id", sa.String(length=255), nullable=True),
            sa.Column("workspace_id", sa.String(length=255), nullable=True),
            sa.Column("agent_name", sa.String(length=255), nullable=False),
            sa.Column("memory_mode", sa.String(length=32), nullable=False),
            sa.Column("session_id", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("provider", sa.String(length=100), nullable=True),
            sa.Column("model", sa.String(length=255), nullable=True),
            sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["token_id"], ["api_tokens.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    if "chat_sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
        if "owner_user_id" not in columns:
            op.add_column("chat_sessions", sa.Column("owner_user_id", sa.String(length=255), nullable=True))
            op.create_foreign_key("fk_chat_sessions_owner_user_id_users", "chat_sessions", "users", ["owner_user_id"], ["id"], ondelete="SET NULL")
        if "workspace_id" not in columns:
            op.add_column("chat_sessions", sa.Column("workspace_id", sa.String(length=255), nullable=True))
            op.create_foreign_key("fk_chat_sessions_workspace_id_workspaces", "chat_sessions", "workspaces", ["workspace_id"], ["id"], ondelete="SET NULL")
        if "created_by_token_id" not in columns:
            op.add_column("chat_sessions", sa.Column("created_by_token_id", sa.String(length=255), nullable=True))
            op.create_foreign_key("fk_chat_sessions_created_by_token_id_api_tokens", "chat_sessions", "api_tokens", ["created_by_token_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "chat_sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
        if "created_by_token_id" in columns:
            op.drop_constraint("fk_chat_sessions_created_by_token_id_api_tokens", "chat_sessions", type_="foreignkey")
            op.drop_column("chat_sessions", "created_by_token_id")
        if "workspace_id" in columns:
            op.drop_constraint("fk_chat_sessions_workspace_id_workspaces", "chat_sessions", type_="foreignkey")
            op.drop_column("chat_sessions", "workspace_id")
        if "owner_user_id" in columns:
            op.drop_constraint("fk_chat_sessions_owner_user_id_users", "chat_sessions", type_="foreignkey")
            op.drop_column("chat_sessions", "owner_user_id")

    for table_name in [
        "agent_run_logs",
        "agent_access_grants",
        "api_tokens",
        "workspace_members",
        "workspaces",
        "users",
    ]:
        if table_name in inspector.get_table_names():
            op.drop_table(table_name)
