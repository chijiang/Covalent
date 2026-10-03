# chat_messages table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize the `transcript_messages` JSONB column on `chat_sessions` into a dedicated `chat_messages` table, migrate existing data, and drop the old column.

**Architecture:** Add a `chat_messages` table (one row per `ChatTranscriptMessage`, ordered by `position`, cascading on session delete). Rewrite only `PersistentSessionStore` to read/write the new table via explicit SQLAlchemy queries (no relationship, matching codebase style). All Pydantic models, `InMemorySessionStore`, and `app.py` stay unchanged. Two migrations: #1 creates `chat_messages`; #2 copies `transcript_messages` into `chat_messages` and drops the column **atomically** (the app auto-migrates to head on startup, so the copy is folded in to avoid data loss). A standalone backfill script is kept as a manual fallback, not a required step.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async (`asyncpg`), PostgreSQL (JSONB), Alembic, `unittest.IsolatedAsyncioTestCase` for async tests, `anyio`.

## Global Constraints

- **Commits are user-driven.** Per the user's standing preference, the implementer must NOT run `git commit`/`git push`. Each task ends with a "Checkpoint" that stages changes and hands off to the user to commit. Do not commit on the user's behalf.
- **Postgres only.** Columns use `postgresql.JSONB`; do not switch to generic `JSON` and do not introduce SQLite for tests.
- **Async test style.** New tests extend `unittest.IsolatedAsyncioTestCase` with `async def test_*` methods. Do not add `pytest`/`pytest-asyncio` or a `conftest.py` — the repo uses `unittest` exclusively.
- **Migration naming/chain.** Date-prefixed revision ids chaining from the current head `20260721_000020`. Every migration is idempotent (guard on existing table/column via `sa.inspect(bind)`), matching `alembic/versions/20260430_000006_create_chat_sessions.py` and `20260708_000014_add_audit_logs.py`.
- **No SQLAlchemy relationships.** The codebase uses explicit `select`/`delete`/`insert` only (see `src/covalent/infra/memory.py`, `src/covalent/infra/config_store.py`). Do not add `relationship()`/`back_populates`.
- **Out of scope.** Do not touch `memory_messages`, `activity`, `app.py`, the streaming logic, `ChatTranscriptMessage`/`ChatSessionRecord`/`ChatSessionSummary` Pydantic models, or `InMemorySessionStore`.

---

## File Structure

- **Create** `alembic/versions/20260724_000021_create_chat_messages.py` — DDL: `chat_messages` table + FK + unique + index (idempotent).
- **Modify** `src/covalent/infra/db.py` — add `ChatMessageRow` model; (Task 7) remove `transcript_messages_json` from `ChatSessionRow`.
- **Create** `tests/test_persistent_session_store.py` — opt-in Postgres integration tests (`@unittest.skipUnless(TEST_DATABASE_URL)`).
- **Modify** `src/covalent/infra/memory.py` — rewrite `PersistentSessionStore` methods to use `chat_messages`; only this class changes.
- **Create** `scripts/backfill_chat_messages.py` — manual fallback: copies `transcript_messages` JSONB → `chat_messages` rows (idempotent). Not required for the cutover, since migration #2 copies atomically.
- **Create** `alembic/versions/20260724_000022_drop_chat_sessions_transcript_messages.py` — DDL: drop the old column (idempotent).

---

## Task 1: Migration #1 — create the `chat_messages` table

**Files:**
- Create: `alembic/versions/20260724_000021_create_chat_messages.py`

**Interfaces:**
- Produces: revision `20260724_000021`, `down_revision = "20260721_000020"` (the current head). Creates table `chat_messages` with columns `id`, `session_id`, `role`, `content`, `attachments` (JSONB), `position` (int); FK `session_id → chat_sessions(id) ON DELETE CASCADE`; unique `(session_id, position)`; index on `session_id`.

- [ ] **Step 1: Write the migration file**

Create `alembic/versions/20260724_000021_create_chat_messages.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260724_000021"
down_revision = "20260721_000020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_messages" in inspector.get_table_names():
        return

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            ondelete="CASCADE",
            name="fk_chat_messages_session_id_chat_sessions",
        ),
        sa.UniqueConstraint("session_id", "position", name="uq_chat_messages_session_id_position"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "chat_messages" not in inspector.get_table_names():
        return
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")
```

- [ ] **Step 2: Verify it applies cleanly against a Postgres DB**

Run (substitute your dev DB; the `+asyncpg` form is for the app, the plain form is what alembic uses):

```bash
DATABASE_URL="postgresql://user:pass@localhost:5432/covalent"
.venv/bin/python -c "
from covalent.infra.migrations import run_database_migrations
run_database_migrations('$DATABASE_URL')
"
.venv/bin/python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    e = create_async_engine('$DATABASE_URL+asyncpg'.replace('postgresql+asyncpg','postgresql+asyncpg'))
    async with e.connect() as c:
        r = await c.execute(text(\"select count(*) from pg_tables where tablename='chat_messages'\"))
        print('chat_messages exists:', r.scalar())
        r = await c.execute(text(\"select indexname from pg_indexes where tablename='chat_messages' order by indexname\"))
        print('indexes:', [x[0] for x in r])
    await e.dispose()
asyncio.run(main())
"
```

Expected: prints `chat_messages exists: 1` and indexes include `ix_chat_messages_session_id` and `uq_chat_messages_session_id_position`. (Re-running the migration must be a no-op due to the guard — verify by running the upgrade twice.)

- [ ] **Step 3: Checkpoint (hand off to user to commit)**

```bash
git add alembic/versions/20260724_000021_create_chat_messages.py
```

Stage only; **do not commit** — the user commits.

---

## Task 2: Add the `ChatMessageRow` ORM model

**Files:**
- Modify: `src/covalent/infra/db.py` (add the class after `ChatSessionRow`, which ends at line 341)

**Interfaces:**
- Produces: `ChatMessageRow` (maps `chat_messages`). Field names consumed by Task 4: `ChatMessageRow.id` (`str`), `.session_id` (`str`), `.role` (`str`), `.content` (`str`), `.attachments` (`list[dict]`), `.position` (`int`).
- Note: `ChatSessionRow.transcript_messages_json` is intentionally kept for now — it is removed in Task 7 together with the column drop.

- [ ] **Step 1: Add the model class**

In `src/covalent/infra/db.py`, immediately after the `ChatSessionRow` class (after its `created_at` line, ~line 341) and before `run_session_operation`, add:

```python
class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_chat_messages_session_id_position"),
    )
```

All imports (`String`, `ForeignKey`, `Text`, `JSONB`, `Integer`, `UniqueConstraint`, `DateTime`, `func`, `Any`, `datetime`, `Mapped`, `mapped_column`) are already present at the top of `db.py` (see lines 3–11).

- [ ] **Step 2: Verify it imports and maps the table**

```bash
.venv/bin/python -c "
from covalent.infra.db import ChatMessageRow
t = ChatMessageRow.__table__
print('table:', t.name)
print('cols:', [c.name for c in t.columns])
print('pk:', [c.name for c in t.primary_key])
"
```

Expected: `table: chat_messages`, `cols: ['id', 'session_id', 'role', 'content', 'attachments', 'position', 'created_at']`, `pk: ['id']`.

- [ ] **Step 3: Checkpoint (hand off to user to commit)**

```bash
git add src/covalent/infra/db.py
```

Stage only; **do not commit**.

---

## Task 3: Integration test harness + smoke test

**Files:**
- Create: `tests/test_persistent_session_store.py`

**Interfaces:**
- Produces: `PersistentSessionStoreTestCase` base class — applies migrations to `TEST_DATABASE_URL`, truncates `chat_messages`/`chat_sessions` before each test, exposes `_store()` returning a `PersistentSessionStore`. Auto-skipped when `TEST_DATABASE_URL` is unset.

- [ ] **Step 1: Write the harness and a smoke test**

Create `tests/test_persistent_session_store.py`:

```python
from __future__ import annotations

import os
import unittest

from sqlalchemy import text

from covalent.infra.db import DatabaseManager
from covalent.infra.migrations import run_database_migrations


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "set TEST_DATABASE_URL to run")
class PersistentSessionStoreTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        run_database_migrations(cls.database_url.replace("+asyncpg", ""))
        cls.db = DatabaseManager(cls.database_url)

    async def asyncSetUp(self) -> None:
        async with self.db.session_factory() as session:
            await session.execute(
                text("TRUNCATE chat_messages, chat_sessions RESTART IDENTITY CASCADE")
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        async with self.db.session_factory() as session:
            await session.execute(
                text("TRUNCATE chat_messages, chat_sessions RESTART IDENTITY CASCADE")
            )
            await session.commit()

    def _store(self):
        from covalent.infra.memory import PersistentSessionStore

        return PersistentSessionStore(self.db.session_factory)

    async def test_chat_messages_table_exists(self) -> None:
        async with self.db.session_factory() as session:
            result = await session.execute(
                text("select count(*) from pg_tables where tablename = 'chat_messages'")
            )
        self.assertEqual(result.scalar(), 1)
```

- [ ] **Step 2: Run the smoke test against a Postgres DB**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -m unittest tests.test_persistent_session_store -v
```

Expected: 1 test, `OK`. (Confirms Task 1 migration applied and the harness connects.)

Also confirm it skips cleanly with no DB:

```bash
.venv/bin/python -m unittest tests.test_persistent_session_store -v
```

Expected: `SKIPPED` (1 test).

- [ ] **Step 3: Checkpoint (hand off to user to commit)**

```bash
git add tests/test_persistent_session_store.py
```

Stage only; **do not commit**.

---

## Task 4: Rewrite `PersistentSessionStore` to use `chat_messages` (TDD core)

**Files:**
- Modify: `src/covalent/infra/memory.py` (only `PersistentSessionStore` and its helpers)
- Test: `tests/test_persistent_session_store.py` (append cases)

**Interfaces:**
- Consumes (from Task 2): `ChatMessageRow`.
- Produces: unchanged `SessionStore`/`PersistentSessionStore` public method signatures (`get_session`, `save_session`, `list_sessions`, `delete_session`, `update_title`, `load_messages`, `save_messages`). `app.py` is untouched and must keep working.

- [ ] **Step 1: Write failing integration tests (round-trip + message_count in summary)**

Append to `tests/test_persistent_session_store.py` (after the smoke test, still inside the file — add these methods to the existing `PersistentSessionStoreTestCase` class; place them before the final `test_chat_messages_table_exists` or after — order does not matter for `unittest`):

```python
    async def test_save_then_get_round_trips_messages_in_order(self) -> None:
        from datetime import UTC, datetime

        from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        record = ChatSessionRecord(
            id="sess-1",
            title="t",
            created_at=now,
            updated_at=now,
            messages=[
                ChatTranscriptMessage(id="m1", role="user", content="hi", attachments=[]),
                ChatTranscriptMessage(
                    id="m2", role="assistant", content="hello", attachments=[{"k": "v"}]
                ),
            ],
        )

        saved = await store.save_session(record)
        self.assertEqual(saved.message_count, 2)

        loaded = await store.get_session("sess-1")
        self.assertIsNotNone(loaded)
        self.assertEqual([m.id for m in loaded.messages], ["m1", "m2"])
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertEqual(loaded.messages[0].content, "hi")
        self.assertEqual(loaded.messages[1].attachments, [{"k": "v"}])
        self.assertEqual(loaded.message_count, 2)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -m unittest tests.test_persistent_session_store.PersistentSessionStoreTestCase.test_save_then_get_round_trips_messages_in_order -v
```

Expected: FAIL — `save_session` still writes the JSONB column and `get_session` still reads it; with the new table empty, `message_count`/`messages` will be wrong (the assertion on `saved.message_count == 2` fails because the store still derives count from the old column, which works only if it still writes there — but after this task it must not). If it happens to pass because the old column path still functions, that's fine; the failure will surface in Step 4 once you've started rewriting. The key is: run it, observe current behavior.

- [ ] **Step 3: Rewrite the store**

In `src/covalent/infra/memory.py`:

3a. Update imports (line 8 and line 12):

```python
from sqlalchemy import delete, desc, func, select
```

```python
from covalent.infra.db import ChatMessageRow, ChatSessionRow, run_session_operation
```

3b. Replace `_summary_from_row` (currently lines 239–253) so `message_count` is supplied by the caller instead of read from the JSONB column:

```python
    @staticmethod
    def _summary_from_row(row: ChatSessionRow, *, message_count: int) -> ChatSessionSummary:
        return ChatSessionSummary(
            id=row.id,
            title=row.title,
            title_source=row.title_source,
            agent_name=row.agent_name,
            owner_user_id=row.owner_user_id,
            workspace_id=row.workspace_id,
            created_by_token_id=row.created_by_token_id,
            preview_text=row.preview_text,
            message_count=message_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
```

3c. Replace `_record_from_row` (currently lines 255–262) — make it `async`, take the `session`, and load messages from the new table (or accept pre-fetched messages):

```python
    @classmethod
    async def _record_from_row(
        cls,
        session: AsyncSession,
        row: ChatSessionRow,
        *,
        messages: list[ChatTranscriptMessage] | None = None,
        message_count: int | None = None,
    ) -> ChatSessionRecord:
        if messages is None:
            messages = await cls._load_messages(session, row.id)
        if message_count is None:
            message_count = len(messages)
        return ChatSessionRecord(
            **cls._summary_from_row(row, message_count=message_count).model_dump(),
            memory_messages=[Message.model_validate(item) for item in row.memory_messages_json],
            messages=messages,
            activity=[ChatActivityItem.model_validate(item) for item in row.activity_json],
        )
```

3d. Add the two new query helpers (place them just above `_summary_from_row`):

```python
    @staticmethod
    async def _load_messages(session: AsyncSession, session_id: str) -> list[ChatTranscriptMessage]:
        stmt = (
            select(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
            .order_by(ChatMessageRow.position)
        )
        rows = list(await session.scalars(stmt))
        return [
            ChatTranscriptMessage(
                id=row.id,
                role=row.role,
                content=row.content,
                attachments=list(row.attachments or []),
            )
            for row in rows
        ]

    @staticmethod
    async def _count_messages(session: AsyncSession, session_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessageRow)
            .where(ChatMessageRow.session_id == session_id)
        )
        return int(await session.scalar(stmt) or 0)
```

3e. Replace `get_session` (currently lines 181–188) to load messages via the new helper:

```python
    async def get_session(self, session_id: str) -> ChatSessionRecord | None:
        async def _get(session: AsyncSession) -> ChatSessionRecord | None:
            row = await session.get(ChatSessionRow, session_id)
            if row is None:
                return None
            return await self._record_from_row(session, row)

        return await run_session_operation(self._session_factory, _get)
```

3f. Replace `list_sessions` (currently lines 169–179) to compute `message_count` per session from the new table:

```python
    async def list_sessions(
        self, *, owner_user_id: str | None = None, workspace_id: str | None = None
    ) -> list[ChatSessionSummary]:
        async def _list(session: AsyncSession) -> list[ChatSessionSummary]:
            stmt = select(ChatSessionRow).order_by(desc(ChatSessionRow.updated_at))
            if owner_user_id is not None:
                stmt = stmt.where(ChatSessionRow.owner_user_id == owner_user_id)
            if workspace_id is not None:
                stmt = stmt.where(ChatSessionRow.workspace_id == workspace_id)
            rows = list(await session.scalars(stmt))
            summaries: list[ChatSessionSummary] = []
            for row in rows:
                count = await self._count_messages(session, row.id)
                summaries.append(self._summary_from_row(row, message_count=count))
            return summaries

        return await run_session_operation(self._session_factory, _list)
```

3g. Replace `save_session` (currently lines 190–213) to delete-then-insert messages in the same transaction, and return the record built from the row + the input messages:

```python
    async def save_session(self, record: ChatSessionRecord) -> ChatSessionRecord:
        async def _save(session: AsyncSession) -> ChatSessionRecord:
            async with session.begin():
                row = await session.get(ChatSessionRow, record.id)
                if row is None:
                    row = ChatSessionRow(id=record.id)
                    session.add(row)
                if record.owner_user_id is not None:
                    row.owner_user_id = record.owner_user_id
                if record.workspace_id is not None:
                    row.workspace_id = record.workspace_id
                if record.created_by_token_id is not None:
                    row.created_by_token_id = record.created_by_token_id
                row.title = record.title
                row.title_source = record.title_source
                row.agent_name = record.agent_name
                row.preview_text = record.preview_text
                row.memory_messages_json = [
                    message.model_dump(mode="json") for message in record.memory_messages
                ]
                row.activity_json = [item.model_dump(mode="json") for item in record.activity]

                await session.execute(
                    delete(ChatMessageRow).where(ChatMessageRow.session_id == record.id)
                )
                for position, message in enumerate(record.messages):
                    session.add(
                        ChatMessageRow(
                            id=message.id,
                            session_id=record.id,
                            role=message.role,
                            content=message.content,
                            attachments=list(message.attachments),
                            position=position,
                        )
                    )
            await session.refresh(row)
            return await self._record_from_row(
                session, row, messages=list(record.messages), message_count=len(record.messages)
            )

        return await run_session_operation(self._session_factory, _save)
```

3h. Replace `update_title` (currently lines 215–226) — `_record_from_row` is now async, so update its call:

```python
    async def update_title(self, session_id: str, title: str, title_source: SessionTitleSource = "manual") -> ChatSessionRecord:
        async def _update(session: AsyncSession) -> ChatSessionRecord:
            async with session.begin():
                row = await session.get(ChatSessionRow, session_id)
                if row is None:
                    raise KeyError(session_id)
                row.title = title
                row.title_source = title_source
            await session.refresh(row)
            return await self._record_from_row(session, row)

        return await run_session_operation(self._session_factory, _update)
```

`delete_session` (lines 228–237) needs **no change** — `ON DELETE CASCADE` removes the messages automatically. Leave it as-is.

- [ ] **Step 4: Run the round-trip test to verify it passes**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -m unittest tests.test_persistent_session_store.PersistentSessionStoreTestCase.test_save_then_get_round_trips_messages_in_order -v
```

Expected: PASS.

- [ ] **Step 5: Checkpoint (hand off to user to commit)**

```bash
git add src/covalent/infra/memory.py tests/test_persistent_session_store.py
```

Stage only; **do not commit**.

---

## Task 5: Store behaviors — overwrite, cascade delete, list count

**Files:**
- Test: `tests/test_persistent_session_store.py` (append cases). No production changes — the Task 4 implementation already covers these; these tests lock in the contract.

**Interfaces:**
- Consumes: `PersistentSessionStore` (Task 4).

- [ ] **Step 1: Write the failing tests**

Append these methods to `PersistentSessionStoreTestCase`:

```python
    async def test_resave_replaces_messages_without_duplicates(self) -> None:
        from datetime import UTC, datetime

        from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[ChatTranscriptMessage(id="m3", role="user", content="again")],
            )
        )

        loaded = await store.get_session("sess-1")
        self.assertEqual([m.id for m in loaded.messages], ["m3"])
        self.assertEqual(loaded.message_count, 1)

        async with self.db.session_factory() as session:
            result = await session.execute(
                text("select count(*) from chat_messages where session_id = 'sess-1'")
            )
        self.assertEqual(result.scalar(), 1)

    async def test_delete_session_removes_messages_via_cascade(self) -> None:
        from datetime import UTC, datetime

        from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="t",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )

        deleted = await store.delete_session("sess-1")
        self.assertTrue(deleted)

        async with self.db.session_factory() as session:
            result = await session.execute(
                text("select count(*) from chat_messages where session_id = 'sess-1'")
            )
        self.assertEqual(result.scalar(), 0)

    async def test_list_sessions_reports_message_count(self) -> None:
        from datetime import UTC, datetime

        from covalent.infra.memory import ChatSessionRecord, ChatTranscriptMessage

        store = self._store()
        now = datetime.now(UTC)
        await store.save_session(
            ChatSessionRecord(
                id="sess-1",
                title="one",
                created_at=now,
                updated_at=now,
                messages=[
                    ChatTranscriptMessage(id="m1", role="user", content="hi"),
                    ChatTranscriptMessage(id="m2", role="assistant", content="hello"),
                ],
            )
        )
        await store.save_session(
            ChatSessionRecord(
                id="sess-2",
                title="two",
                created_at=now,
                updated_at=now,
                messages=[],
            )
        )

        sessions = await store.list_sessions()
        by_id = {s.id: s.message_count for s in sessions}
        self.assertEqual(by_id.get("sess-1"), 2)
        self.assertEqual(by_id.get("sess-2"), 0)
```

- [ ] **Step 2: Run them — they should pass already**

```bash
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -m unittest tests.test_persistent_session_store -v
```

Expected: all 5 tests PASS (smoke + round-trip + overwrite + cascade + list count). If any fails, fix in `memory.py` before proceeding — do not move on with a red test.

- [ ] **Step 3: Checkpoint (hand off to user to commit)**

```bash
git add tests/test_persistent_session_store.py
```

Stage only; **do not commit**.

---

## Task 6: Ad hoc backfill script (with unit-tested transform)

**Files:**
- Create: `scripts/backfill_chat_messages.py`

**Interfaces:**
- Produces: `rows_from_transcript(session_id: str, transcript: list[dict]) -> list[dict]` (pure, unit-tested) and an async `main()` DB driver. The script reads `transcript_messages` via raw SQL (not the ORM) so it is unaffected by Task 7 dropping the column from `ChatSessionRow`; it writes via `ChatMessageRow` + `on_conflict_do_nothing` (correct JSONB serialization + idempotency).

- [ ] **Step 1: Write a failing unit test for the transform**

Append to `tests/test_persistent_session_store.py` a new (non-DB, always-run) test class at module level — it must NOT be inside `PersistentSessionStoreTestCase` (so it runs without `TEST_DATABASE_URL`):

```python
class RowsFromTranscriptTest(unittest.TestCase):
    def test_maps_fields_and_assigns_position(self) -> None:
        from scripts.backfill_chat_messages import rows_from_transcript

        transcript = [
            {"id": "user-1", "role": "user", "content": "hi", "attachments": []},
            {"id": "assistant-1", "role": "assistant", "content": "hello", "attachments": [{"k": "v"}]},
        ]
        self.assertEqual(
            rows_from_transcript("sess-1", transcript),
            [
                {
                    "id": "user-1",
                    "session_id": "sess-1",
                    "role": "user",
                    "content": "hi",
                    "attachments": [],
                    "position": 0,
                },
                {
                    "id": "assistant-1",
                    "session_id": "sess-1",
                    "role": "assistant",
                    "content": "hello",
                    "attachments": [{"k": "v"}],
                    "position": 1,
                },
            ],
        )

    def test_empty_transcript_returns_empty(self) -> None:
        from scripts.backfill_chat_messages import rows_from_transcript

        self.assertEqual(rows_from_transcript("sess-1", []), [])

    def test_missing_attachments_defaults_to_empty_list(self) -> None:
        from scripts.backfill_chat_messages import rows_from_transcript

        rows = rows_from_transcript("sess-1", [{"id": "a", "role": "user", "content": "c"}])
        self.assertEqual(rows[0]["attachments"], [])
```

(`scripts/` must be importable as a package. If `python -m unittest` cannot import `scripts.backfill_chat_messages`, create an empty `scripts/__init__.py`. Check first; only add it if the import fails.)

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m unittest tests.test_persistent_session_store.RowsFromTranscriptTest -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.backfill_chat_messages'`.

- [ ] **Step 3: Write the backfill script**

Create `scripts/backfill_chat_messages.py`:

```python
"""Ad hoc backfill: copy chat_sessions.transcript_messages (JSONB) -> chat_messages rows.

Run AFTER migration 20260724_000021 (creates chat_messages) and BEFORE migration
20260724_000022 (drops the transcript_messages column).

    DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db" \
        .venv/bin/python -m scripts.backfill_chat_messages

Idempotent: re-running inserts only rows whose id is not already present
(ON CONFLICT (id) DO NOTHING).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from covalent.infra.db import ChatMessageRow, DatabaseManager


def rows_from_transcript(session_id: str, transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a transcript JSONB array to chat_messages row dicts with stable positions."""
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(transcript or []):
        rows.append(
            {
                "id": item["id"],
                "session_id": session_id,
                "role": item["role"],
                "content": item["content"],
                "attachments": list(item.get("attachments") or []),
                "position": position,
            }
        )
    return rows


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    db = DatabaseManager(database_url)

    async with db.session_factory() as session:
        sessions = (
            await session.execute(text("SELECT id, transcript_messages FROM chat_sessions"))
        ).all()

    total = 0
    for session_id, transcript in sessions:
        rows = rows_from_transcript(session_id, transcript or [])
        if not rows:
            continue
        async with db.session_factory() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(ChatMessageRow)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
        total += len(rows)

    print(f"Backfilled {total} chat_messages rows across {len(sessions)} sessions.")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run the unit test to verify it passes**

```bash
.venv/bin/python -m unittest tests.test_persistent_session_store.RowsFromTranscriptTest -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

```bash
.venv/bin/python -m unittest tests.test_persistent_session_store -v
```

Expected: all tests PASS (`RowsFromTranscriptTest` runs unconditionally; `PersistentSessionStoreTestCase` runs only if `TEST_DATABASE_URL` is set, else skips).

- [ ] **Step 6: Manual integration run against a dev DB with existing data**

Against a DB that still has the `transcript_messages` column populated (i.e. after migration #1 applied, before migration #2):

```bash
DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent" \
.venv/bin/python -m scripts.backfill_chat_messages
```

Expected: prints `Backfilled N chat_messages rows across M sessions.` Re-run it — the second run should report the same `N` (idempotent via `ON CONFLICT`). Spot-check with:

```bash
psql "postgresql://user:pass@localhost:5432/covalent" -c \
  "select session_id, count(*) from chat_messages group by session_id order by session_id limit 10;"
```

- [ ] **Step 7: Checkpoint (hand off to user to commit)**

```bash
git add scripts/backfill_chat_messages.py tests/test_persistent_session_store.py
# also add scripts/__init__.py if you had to create it
```

Stage only; **do not commit**.

---

## Task 7: Migration #2 — drop the `transcript_messages` column + remove ORM attribute

**Files:**
- Create: `alembic/versions/20260724_000022_drop_chat_sessions_transcript_messages.py`
- Modify: `src/covalent/infra/db.py` — remove the `transcript_messages_json` attribute from `ChatSessionRow` (lines 334–339).

**Interfaces:**
- Consumes: Task 4 store (no longer references `transcript_messages_json`). Task 6 backfill already run against any DB you care about.
- Produces: revision `20260724_000022`, `down_revision = "20260724_000021"`. The `transcript_messages` column and its ORM mapping are gone; `chat_messages` is the sole source of truth.

**Precondition:** The backfill script (Task 6) has been run on every environment whose historical data you want to keep. Dropping the column discards the JSONB irreversibly (rollback = restore from DB backup).

- [ ] **Step 1: Write the migration**

Create `alembic/versions/20260724_000022_drop_chat_sessions_transcript_messages.py`:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260724_000022"
down_revision = "20260724_000021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "transcript_messages" not in columns:
        return
    op.drop_column("chat_sessions", "transcript_messages")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "transcript_messages" in columns:
        return
    op.add_column(
        "chat_sessions",
        sa.Column(
            "transcript_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
```

- [ ] **Step 2: Remove the ORM attribute**

In `src/covalent/infra/db.py`, delete the `transcript_messages_json` attribute from `ChatSessionRow` (lines 334–339):

```python
    transcript_messages_json: Mapped[list[dict[str, Any]]] = mapped_column(
        "transcript_messages",
        JSONB,
        nullable=False,
        default=list,
    )
```

(Delete the whole block. The preceding `memory_messages_json` line and the following `activity_json` line remain.)

- [ ] **Step 3: Confirm the store no longer references the attribute**

```bash
grep -n "transcript_messages" src/covalent/infra/memory.py src/covalent/infra/db.py
```

Expected: **no output** (no references remain in these two files). If any line appears, remove it before continuing.

- [ ] **Step 4: Run the full integration suite against the migrated DB**

Apply the migration, then run tests:

```bash
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -c "
from covalent.infra.migrations import run_database_migrations
import os
run_database_migrations(os.environ['TEST_DATABASE_URL'].replace('+asyncpg',''))
"
TEST_DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/covalent_test" \
.venv/bin/python -m unittest tests.test_persistent_session_store -v
```

Expected: all tests PASS on the schema where `transcript_messages` no longer exists. This proves the store does not depend on the dropped column.

- [ ] **Step 5: Run the broader test suite to catch any other reference**

```bash
.venv/bin/python -m unittest discover -s tests -v 2>&1 | tail -30
```

Expected: no new failures vs. the pre-change baseline. (Pre-existing failures unrelated to this change are out of scope; investigate only failures mentioning `transcript_messages`, `ChatSessionRow`, or the store.)

- [ ] **Step 6: Checkpoint (hand off to user to commit)**

```bash
git add alembic/versions/20260724_000022_drop_chat_sessions_transcript_messages.py src/covalent/infra/db.py
```

Stage only; **do not commit**.

---

## Rollout

The data copy is folded into migration 20260724_000022, so `upgrade head`
(create table -> copy transcript_messages into chat_messages -> drop column)
is data-safe and atomic. On deploy, just restart the app — its startup runs
`run_database_migrations` to head automatically.

The standalone `scripts/backfill_chat_messages.py` is now an optional manual
fallback (re-run or partial backfill); it is no longer required for the
cutover. If used, run it with `AGENT_FRAMEWORK_DATABASE_URL=...` pointing at
the same DB the app uses.

Rollback (emergency): the downgrade re-adds an empty transcript_messages
column (lossy); restore from a DB backup taken before the upgrade to recover
history.
