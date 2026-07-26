# Design: Normalize `transcript_messages` into a `chat_messages` table

**Date:** 2026-07-24
**Status:** Approved (pre-implementation)
**Scope:** Split the `transcript_messages` JSONB column on `chat_sessions` into a
normalized `chat_messages` table, and backfill existing data. `memory_messages` and
`activity` are out of scope and remain JSONB.

## Background

Today every chat message lives inside the `transcript_messages` JSONB column on the
`chat_sessions` row (`src/agent_framework/infra/db.py:334`). Each row carries its entire
conversation as a JSON array of `ChatTranscriptMessage` objects:

```python
# src/agent_framework/infra/memory.py:18
class ChatTranscriptMessage(BaseModel):
    id: str                                  # _new_chat_item_id → "{prefix}-{epoch_ms}-{uuid8}"
    role: Literal["user", "assistant"]
    content: str
    attachments: list[dict[str, Any]]        # heterogeneous → stays JSONB
```

Access pattern is **read-all / write-all per session**: `PersistentSessionStore.save_session`
(`memory.py:208`) overwrites the whole list each run; `get_session` loads the whole record;
`message_count` is `len(transcript_messages_json)` (`memory.py:250`). `delete_session`
deletes the session row.

The app relies on `ChatTranscriptMessage.id` for upsert-by-id during streaming
(`app.py:1229`, `app.py:1233`). IDs are globally unique (`_new_chat_item_id` in
`app.py:3759` uses a uuid suffix).

## Goal / non-goals

- **Goal:** A normalized `chat_messages` table is the sole source of truth for transcript
  messages. Historical JSONB data is migrated. The old column is dropped.
- **Non-goals:** Move `memory_messages` or `activity`. Change `app.py`, the streaming
  logic, the Pydantic models, or the `InMemorySessionStore`. Add per-message timestamps.

## Design

### 1. New table schema

```sql
CREATE TABLE chat_messages (
    id          VARCHAR(255) PRIMARY KEY,    -- = ChatTranscriptMessage.id (globally unique)
    session_id  VARCHAR(255) NOT NULL,
    role        VARCHAR(16)  NOT NULL,       -- 'user' | 'assistant'
    content     TEXT         NOT NULL,
    attachments JSONB        NOT NULL DEFAULT '[]',
    position    INTEGER      NOT NULL,       -- old array index → preserves conversation order
    CONSTRAINT fk_chat_messages_session_id_chat_sessions
        FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, position)            -- no duplicate ordering within a session
);
CREATE INDEX ix_chat_messages_session_id ON chat_messages(session_id);
```

- **Name:** `chat_messages` — consistent with `chat_sessions` / `ChatTranscriptMessage`.
- **PK = the app message id** (reused directly, not a surrogate). It is already globally
  unique, so `ChatTranscriptMessage.id ↔ chat_messages.id` maps with zero friction and the
  existing upsert-by-id logic in `app.py` is unchanged.
- **`position`** faithfully preserves the old array order. No new `created_at` is invented.
- `attachments` keeps the existing heterogeneous JSONB shape.

### 2. ORM changes (`src/agent_framework/infra/db.py`)

- Add `ChatMessageRow(Base)` mapping the table above.
- Remove `transcript_messages_json` from `ChatSessionRow` (dropped in migration #2).
- **No SQLAlchemy relationship.** The codebase uses explicit queries throughout (no
  relationships exist in `db.py`). Match that style: explicit `SELECT`/`DELETE`/`INSERT`,
  which also avoids async lazy-load pitfalls.

### 3. Store layer (`src/agent_framework/infra/memory.py`) — `PersistentSessionStore` only

- `InMemorySessionStore`, `ChatSessionRecord`, `ChatTranscriptMessage`, `ChatSessionSummary`
  are **unchanged** → `app.py` and the streaming logic are untouched.
- `save_session` (`memory.py:208`): replace the single JSONB assignment with
  **delete-existing-then-insert-all** for that `session_id`, inside the same transaction.
  This preserves the overwrite-all semantics.
- `get_session` / `_record_from_row` (`memory.py:260`): load messages via
  `SELECT … WHERE session_id=? ORDER BY position`.
- `_summary_from_row` (`memory.py:250`): `message_count` via `SELECT COUNT(*)` rather than
  `len(transcript_messages_json)`.
- `delete_session` (`memory.py:228`): **no change** — `ON DELETE CASCADE` removes the rows.

### 4. Migration + backfill (clean-cut cutover)

- **Migration #1** — `alembic/versions/20260724_000021_create_chat_messages.py`
  (`down_revision = "20260721_000020"`): `CREATE TABLE chat_messages` + FK + index.
  Idempotent guard (skip if table exists). Leaves `transcript_messages` untouched.
- **Ad hoc backfill** — `scripts/backfill_chat_messages.py`: for each `chat_sessions` row,
  parse `transcript_messages` JSONB and `INSERT` one row per message with
  `position = array index`. Idempotent via `ON CONFLICT (id) DO NOTHING`. Reuses
  `DatabaseManager(settings.database_url)`. Run manually between the two migrations.
- **Migration #2** — `alembic/versions/20260724_000022_drop_chat_sessions_transcript_messages.py`
  (`down_revision = "20260724_000021"`): `DROP COLUMN transcript_messages` from
  `chat_sessions`. Idempotent guard.

### 5. Rollout order (manual coordination)

1. Apply migration #1 (table exists).
2. Deploy the store-layer code change (reads/writes `chat_messages`).
3. Run the backfill script.
4. Apply migration #2 (drop the column).

Between steps 2 and 3, pre-existing sessions appear empty until backfilled; new
conversations work from step 2 onward. This is acceptable for an ad hoc / local cutover.

## Rollback

After migration #2 drops the column, the JSONB is gone — rollback requires restoring from a
DB backup taken before step 4, then reverting the code. (Accepted as part of the clean-cut
choice.) Before step 4, rollback is trivial: revert code, old column still intact.

## Testing

Extend coverage for `PersistentSessionStore` (referenced by `tests/test_public_invoke_api.py`
and `tests/test_sandbox_admin_api.py`):

- Round-trip a session with mixed user/assistant messages + attachments → reload → assert
  order, roles, and `attachments` preserved.
- Re-save the same session with a different message set → assert overwrite (no duplicates,
  correct `position`).
- Delete a session → assert its `chat_messages` rows are gone (cascade).
- `message_count` reflects the new table.
