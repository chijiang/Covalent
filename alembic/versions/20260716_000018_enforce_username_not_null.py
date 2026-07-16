from __future__ import annotations

"""enforce username NOT NULL (unique index already added in 20260710_000017)"""

import re

from alembic import op
import sqlalchemy as sa


revision = "20260716_000018"
down_revision = "20260710_000017"
branch_labels = None
depends_on = None


# Mirrors agent_framework.api.schemas.USERNAME_PATTERN. Duplicated here so the
# migration stays self-contained (it must not import application code).
_USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")


def _derive_base(local_part: str) -> str:
    base = re.sub(r"[^a-z0-9_-]", "-", (local_part or "").lower()).strip("-_")
    if not _USERNAME_PATTERN.match(base):
        base = "user"
    # Leave room for a "-NNN" suffix within the 32-char limit.
    return base[:27]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return

    # The case-insensitive unique index was added in 20260710_000017. Recreate it
    # defensively for databases that predate that revision so the uniqueness rule
    # holds once the column becomes NOT NULL.
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "uq_users_username_lower" not in indexes:
        op.create_index(
            "uq_users_username_lower",
            "users",
            [sa.text("lower(username)")],
            unique=True,
        )

    # Backfill any missing usernames from the email local-part before making the
    # column NOT NULL. SQL UNIQUE allows multiple NULLs, which is how a second
    # admin with username=NULL slipped in; remove the NULLs so they can no longer
    # bypass the rule. Each derived value is made unique within the table.
    rows = bind.execute(
        sa.text("SELECT id, email FROM users WHERE username IS NULL OR username = ''")
    ).fetchall()
    for uid, email in rows:
        base = _derive_base((email or "").split("@", 1)[0])
        candidate = base
        suffix = 1
        while (
            bind.execute(
                sa.text(
                    "SELECT 1 FROM users WHERE lower(username) = lower(:c) AND id <> :uid LIMIT 1"
                ),
                {"c": candidate, "uid": uid},
            ).fetchfirst()
            is not None
        ):
            candidate = f"{base}-{suffix}"[-32:]
            suffix += 1
        bind.execute(
            sa.text("UPDATE users SET username = :c WHERE id = :uid"),
            {"c": candidate, "uid": uid},
        )

    op.alter_column(
        "users",
        "username",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    op.alter_column(
        "users",
        "username",
        existing_type=sa.String(length=64),
        nullable=True,
    )
