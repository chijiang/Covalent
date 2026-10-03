# Design: Normalize `activity` into a `chat_activity` table

**Date:** 2026-07-27
**Status:** Approved (pre-implementation)
**Builds on:** the `chat_messages` refactor (`docs/superpowers/specs/2026-07-24-chat-messages-table-design.md`, merged to `main`).
**Scope:** Split the `activity` JSONB column on `chat_sessions` into a normalized `chat_activity` table. `memory_messages` is out of scope (windowed/bounded — no growth problem). Caching is out of scope (low access frequency; the real cost is write amplification, which this split addresses).

## Background

`activity` is the per-session event log: one `ChatActivityItem` per tool result, trace event, error, input-resolution, etc.

```python
# src/covalent/infra/memory.py:25
class ChatActivityItem(BaseModel):
    id: str                    # _new_chat_item_id -> "{prefix}-{epoch_ms}-{uuid8}"
    title: str                 # event name (SSE_EVENT_*)
    payload: Any = None        # heterogeneous -> stays JSONB
```

Access pattern is **read-all / write-all per session**, identical to the old `transcript_messages`:
- `get_session` loads the whole `activity_json` (`memory.py:321`); `save_session` rewrites the whole list each run (`memory.py:214`).
- The run endpoint builds the full activity list in memory during streaming (`app.py:1199`) and saves it once in the `finally` block (`app.py:1266`).

Two properties that make `activity` a strong normalization candidate — stronger than `transcript_messages`:
1. **Unbounded growth.** Every tool call / trace event / error appends an item; there is no windowing (contrast `memory_messages`, which is windowed by `session_history_limit`). A long session can accumulate dozens of items per turn.
2. **Strictly append-only.** The code only ever `.append`s (`app.py:1207/1235/1247/1252`); items are never edited or removed. This enables an idempotent-append write strategy with no delete.

The motivating cost is **write amplification**: each turn rewrites the entire `activity` JSONB blob even though only a few items are new. Normalizing to rows + idempotent append makes a turn write only its new items.

## Goal / non-goals

- **Goal:** `chat_activity` is the sole source of truth for activity; writes are idempotent appends (`ON CONFLICT (id) DO NOTHING`) so a turn writes only net-new rows; the old column is dropped; historical data is migrated atomically.
- **Non-goals:** move `memory_messages`. Add caching. Change `app.py`, the streaming logic, the Pydantic models, or `InMemorySessionStore`. Change the `SessionStore` public API.

## Design

### 1. New table schema

```sql
CREATE TABLE chat_activity (
    id          VARCHAR(255) PRIMARY KEY,    -- = ChatActivityItem.id (globally unique)
    session_id  VARCHAR(255) NOT NULL,
    title       VARCHAR(64)  NOT NULL,       -- event name
    payload     JSONB,                       -- nullable; matches payload: Any = None
    position    INTEGER      NOT NULL,       -- array index -> preserves order
    CONSTRAINT fk_chat_activity_session_id_chat_sessions
        FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, position)
);
CREATE INDEX ix_chat_activity_session_id ON chat_activity(session_id);
```

- **Name:** `chat_activity` (consistent with `chat_sessions` / `chat_messages` / `ChatActivityItem`).
- **PK = the app item id** (globally unique via `_new_chat_item_id`), same zero-friction mapping as `chat_messages`.
- **`payload` is nullable JSONB** — the model field is `payload: Any = None`, and historical JSONB may contain JSON null.
- **`position`** preserves the array order (no per-item timestamp; YAGNI).

### 2. ORM changes (`src/covalent/infra/db.py`)

- Add `ChatActivityRow(Base)` mapping the table. Fields: `id`, `session_id` (FK `chat_sessions.id` `ON DELETE CASCADE`), `title` (`String(64)`), `payload` (`JSONB`, nullable), `position` (`Integer`). `__table_args__` carries `UniqueConstraint("session_id", "position", name="uq_chat_activity_session_id_position")`.
- Remove `activity_json` from `ChatSessionRow` (dropped in migration #2).
- No SQLAlchemy relationship — explicit queries, matching codebase style.

### 3. Store layer (`src/covalent/infra/memory.py`) — `PersistentSessionStore` only

- `InMemorySessionStore`, `ChatActivityItem`, `ChatSessionRecord`, `ChatSessionSummary` are **unchanged** → `app.py` and the streaming logic are untouched.
- Add `_load_activity(session, session_id)` -> `SELECT … WHERE session_id=? ORDER BY position` -> `list[ChatActivityItem]`.
- `save_session`: replace `row.activity_json = [...]` with an **idempotent append**:
  ```python
  await session.execute(
      pg_insert(ChatActivityRow)
      .values([{"id": a.id, "session_id": record.id, "title": a.title,
                "payload": a.payload, "position": pos}
               for pos, a in enumerate(record.activity)])
      .on_conflict_do_nothing(index_elements=["id"])
  )
  ```
  No `delete`. Because activity is strictly append-only and the incoming list is always a superset of stored rows, existing ids are no-ops and only net-new rows are written — O(new items), not O(total). Re-saves are safe (idempotent).
- `_record_from_row`: load activity via `_load_activity` (same shape as `_load_messages`); `save_session` returns a record built from the row + the input `record.activity`.
- `delete_session`: no change (`ON DELETE CASCADE` removes rows).

### 4. Migrations (atomic cutover, mirroring `chat_messages`)

- **Migration #1** — `alembic/versions/20260727_000023_create_chat_activity.py` (`down_revision = "20260724_000022"`, the current `main` head): create `chat_activity` + FK + unique + index, idempotent guard. Leaves `activity` column untouched.
- **Migration #2** — `alembic/versions/20260727_000024_drop_chat_sessions_activity.py` (`down_revision = "20260727_000023"`): in one `upgrade()`, copy `activity` JSONB into `chat_activity` via `INSERT … SELECT … jsonb_array_elements(…) WITH ORDINALITY` (`position = ordinality - 1`, `ON CONFLICT (id) DO NOTHING`), then `DROP COLUMN activity`. Atomic + data-safe (same proven pattern as `20260724_000022`). The app auto-applies `upgrade head` on startup, so deploy = restart. Downgrade re-adds an empty column (lossy; rollback = DB backup).

No standalone backfill script — the copy is folded into migration #2 (the lesson from `chat_messages`: the app auto-migrates to head, so a manual in-between step has no window).

### 5. Testing

Extend `tests/test_persistent_session_store.py` (the harness from the `chat_messages` work, already on `main`):
- **Round-trip:** save a session with several activity items (mixed `title`/`payload`, incl. a `None` payload) → reload → assert order, titles, payloads.
- **Idempotent append:** save with items A,B; save again with A,B,C,D (superset) → assert chat_activity has A,B,C,D exactly once (A,B not duplicated; C,D added) — proves `ON CONFLICT DO NOTHING` and O(new) behavior.
- **Cascade:** delete a session → its `chat_activity` rows are gone.

## Rollback

After migration #2 drops the column, rollback requires a pre-upgrade DB backup (downgrade is lossy). Before #2, rollback is trivial (revert code; column still present).

## Rollout

Deploy + restart the app. Startup runs `run_database_migrations` to head: migration #1 creates `chat_activity`; migration #2 copies `activity` into it and drops the column, atomically.
