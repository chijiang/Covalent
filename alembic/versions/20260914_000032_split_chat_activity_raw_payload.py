"""Split raw model payloads out of chat_activity.payload into raw_payload.

List reads previously transferred and parsed the full payload including
raw_request/raw_response blobs (the bulk of the row size, growing with
conversation length). Keeping them in a separate column makes list reads
cheap; the detail endpoint serves raw_payload on demand.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260914_000032"
down_revision = "20260911_000031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_activity", sa.Column("raw_payload", JSONB, nullable=True))
    op.execute(
        """
        UPDATE chat_activity
        SET raw_payload = jsonb_strip_nulls(jsonb_build_object(
            'raw_request', payload->'raw_request',
            'raw_response', payload->'raw_response'
        ))
        WHERE jsonb_typeof(payload) = 'object'
          AND (payload ? 'raw_request' OR payload ? 'raw_response')
        """
    )
    op.execute(
        """
        UPDATE chat_activity
        SET payload = payload - 'raw_request' - 'raw_response'
        WHERE jsonb_typeof(payload) = 'object'
          AND (payload ? 'raw_request' OR payload ? 'raw_response')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE chat_activity
        SET payload = CASE
            WHEN jsonb_typeof(payload) = 'object' THEN payload || COALESCE(raw_payload, '{}'::jsonb)
            ELSE payload
        END
        WHERE raw_payload IS NOT NULL
        """
    )
    op.drop_column("chat_activity", "raw_payload")
