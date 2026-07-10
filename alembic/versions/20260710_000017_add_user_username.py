from __future__ import annotations

"""add user username"""

from alembic import op
import sqlalchemy as sa


revision = "20260710_000017"
down_revision = "20260710_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "username" not in columns:
        op.add_column("users", sa.Column("username", sa.String(length=64), nullable=True))

    # Backfill existing rows so the unique index can be created without conflicts.
    op.execute("UPDATE users SET username = email WHERE username IS NULL")

    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "uq_users_username_lower" not in indexes:
        op.create_index(
            "uq_users_username_lower",
            "users",
            [sa.text("lower(username)")],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "uq_users_username_lower" in indexes:
        op.drop_index("uq_users_username_lower", table_name="users")
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "username" in columns:
        op.drop_column("users", "username")
