# Edit-and-resend for user messages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ChatGPT-style edit-and-resend: editing a user message truncates the server-side transcript from that message onward, then regenerates; a toast offers Undo to restore the discarded tail.

**Architecture:** The agent stream always rebuilds conversation from server-stored `session.messages`. So edit-and-resend = truncate the stored transcript to the edited message's predecessor, then call the existing `/agents/{name}/stream` unchanged. One new backend endpoint `PUT /sessions/{id}/transcript` performs both the truncate (edit) and the explicit-replace (undo), reusing the existing `save_session` DELETE+re-insert path (no migration). Frontend adds an inline edit affordance on user bubbles and a sonner toast for Undo, reusing the existing `runThreadRequest` for regeneration.

**Tech Stack:** Python 3 / FastAPI / Pydantic / SQLAlchemy (backend); Next.js + React + TypeScript + sonner (frontend, already wired in `providers.tsx`).

## Global Constraints

- No DB migration — `PersistentSessionStore.save_session` already does `DELETE FROM chat_messages WHERE session_id=?` then re-inserts (`src/covalent/infra/memory.py:225-238`). The new endpoint reuses it.
- Console-only feature — the public invoke API and API-token runs are untouched; `AgentRunRequest` is unchanged.
- Owner authorization — every session access applies `_ensure_console_principal_can_access_session` (the existing check used by the other session routes).
- Activity rows are append-only (`on_conflict_do_nothing`, `memory.py:241-257`). On edit, messages are truncated but old activity/trace rows are left as-is (v1 boundary; add a one-line comment on the route).
- No auto-commit of the work (user preference). The plan still shows `git add` / `git commit` steps as the natural commit boundaries per task; the user runs them.
- Frontend Next.js note: `frontend/AGENTS.md` says this Next.js has breaking changes — read the relevant `node_modules/next/dist/docs/` guide before writing any Next.js code. This feature is plain React state + fetch (no routing/App-Router conventions), so risk is low; still verify before touching anything Next-specific.

---

## File Structure

**Backend (Python):**
- Modify `src/covalent/application/schemas.py` — add `ChatTranscriptMessageInput` and `TranscriptReplaceRequest` request schemas (next to the existing chat schemas ~L389).
- Modify `src/covalent/api/routes/sessions.py` — add the `PUT /sessions/{id}/transcript` route (next to the existing `PATCH /sessions/{id}` rename route ~L65).
- `src/covalent/api/_shared.py` already provides `to_chat_session_response` (used to build the response) — no change needed, just consumed.
- `src/covalent/application/services/session_service.py` already exports `_build_session_preview` — no change, just imported.
- Create `tests/test_session_transcript_replace_api.py` — route-level tests for the new endpoint.

**Frontend (TypeScript):**
- Modify `frontend/lib/client-api.ts` — add `replaceChatTranscript()` + `ChatTranscriptMessageInput` type.
- Modify `frontend/lib/types.ts` — only if `ChatSession`/`ChatSessionMessage` types need a field; verify (likely no change — ids already present).
- Modify `frontend/components/chat-workspace.tsx` — add inline edit UI on `ChatMessageBubble`, `handleEditResend`, undo state + `handleUndoEdit`, sonner toast.

---

## Task 1: Backend request schemas

**Files:**
- Modify: `src/covalent/application/schemas.py` (insert after `ChatSessionUpdateRequest`, ~L391)

**Interfaces:**
- Produces: `ChatTranscriptMessageInput` and `TranscriptReplaceRequest` (Pydantic models), consumed by Task 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_transcript_replace_api.py` with a schema-validation test only (route comes in Task 2):

```python
"""Tests for PUT /sessions/{id}/transcript (edit-and-resend)."""

from __future__ import annotations

import unittest

from covalent.application.schemas import (
    ChatTranscriptMessageInput,
    TranscriptReplaceRequest,
)


class TranscriptReplaceRequestSchemaTestCase(unittest.TestCase):
    def test_truncate_mode(self) -> None:
        req = TranscriptReplaceRequest(truncate_before_message_id="um-2")
        self.assertEqual(req.truncate_before_message_id, "um-2")
        self.assertIsNone(req.messages)

    def test_explicit_replace_mode(self) -> None:
        msg = ChatTranscriptMessageInput(id="um-1", role="user", content="hi")
        req = TranscriptReplaceRequest(messages=[msg])
        self.assertEqual(req.messages, [msg])
        self.assertIsNone(req.truncate_before_message_id)

    def test_message_attachments_default_empty(self) -> None:
        msg = ChatTranscriptMessageInput(id="um-1", role="user", content="hi")
        self.assertEqual(msg.attachments, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'ChatTranscriptMessageInput'` (or `TranscriptReplaceRequest`).

- [ ] **Step 3: Write minimal implementation**

In `src/covalent/application/schemas.py`, insert immediately after the `ChatSessionUpdateRequest` class (after its closing line, before the `# --- Skill management schemas ---` comment):

```python
class ChatTranscriptMessageInput(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class TranscriptReplaceRequest(BaseModel):
    # Exactly one of the two must be supplied:
    #   truncate_before_message_id -> keep messages strictly before this id (edit-and-resend)
    #   messages                   -> explicit full replace (undo)
    truncate_before_message_id: str | None = None
    messages: list[ChatTranscriptMessageInput] | None = None
```

Note: `Literal` and `BaseModel`, `Field`, `Any` are already imported at the top of this file (used by the surrounding schemas) — confirm before saving; if `Any` is missing, add `from typing import Any` (it is already imported given existing usage like `payload: Any = None`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/covalent/application/schemas.py tests/test_session_transcript_replace_api.py
git commit -m "feat: add TranscriptReplaceRequest schemas for session transcript replace"
```

---

## Task 2: Backend `PUT /sessions/{id}/transcript` route

**Files:**
- Modify: `src/covalent/api/routes/sessions.py` (add route after `rename_session`, ~L78; add imports at top)

**Interfaces:**
- Consumes: `TranscriptReplaceRequest` (Task 1); `to_chat_session_response` (already imported in this file via `from covalent.api._shared import to_chat_session_response`); `_build_session_preview` (import from `covalent.application.services.session_service`); `_ensure_console_principal_can_access_session` (already imported); `_resolve_console_principal` (already imported); `ChatSessionRecord`, `ChatTranscriptMessage` from `covalent.infra.memory`.
- Produces: `PUT /sessions/{session_id}/transcript` returning `ChatSessionResponse`. Consumed by Task 4 (frontend).

- [ ] **Step 1: Write the failing test**

Append a route-level test class to `tests/test_session_transcript_replace_api.py`. It builds the app the same way `tests/test_agent_crud_api.py` does (`create_app()` + swap `app.state.*` with fakes + an admin fake-DB so `_resolve_console_principal` yields an admin and the owner check passes under `console_auth_mode="local"` with anonymous requests), but swaps in a **real** `InMemorySessionStore` so messages persist. Sessions are seeded directly through the store.

Add these imports to the top of the file (after the existing ones):

```python
from datetime import datetime, UTC
from types import SimpleNamespace

from starlette.testclient import TestClient

from covalent.api.app import create_app
from covalent.infra.memory import (
    ChatSessionRecord,
    ChatTranscriptMessage,
    InMemorySessionStore,
)
from covalent.infra.settings import AppSettings
```

Add the fake-DB + app builder (copy the proven pattern from `tests/test_agent_crud_api.py:38-104`; the `_FakeDbSession.get` returns an admin `UserRow` so the resolved principal `is_admin` is true):

```python
class _FakeTransaction:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _FakeDbSession:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass

    def begin(self):
        return _FakeTransaction()

    async def get(self, model, key):
        if model.__name__ == "UserRow":
            return SimpleNamespace(
                id=key or "admin", email="admin@local", display_name="Local Admin",
                role="admin", status="active", avatar_url=None, username="admin",
                preferences_json={},
            )
        if model.__name__ == "AgentRow":
            return SimpleNamespace(
                name=key or "default", display_name="Default Agent",
                owner_user_id="admin", visibility="public", publication_status="approved",
            )
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalar(self, *a, **kw):
        return SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            name="Workspace 1", slug="ws-1", role="admin",
        )

    async def scalars(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return None

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


def _build_app_with_store(store: InMemorySessionStore):
    app = create_app()
    app.state.settings = AppSettings(console_auth_mode="local", workspace_root_dir="/tmp")
    app.state.db_manager = SimpleNamespace(session_factory=lambda: _FakeDbSession())
    app.state.registry = SimpleNamespace()
    app.state.runtime = SimpleNamespace()
    app.state.config_store = SimpleNamespace()
    app.state.execution_backend = SimpleNamespace(name="filesystem")
    app.state.skill_loader = SimpleNamespace()
    app.state.session_store = store
    return TestClient(app)


def _seed_session(store: InMemorySessionStore, session_id: str) -> None:
    """Seed [um-1 user, am-1 assistant, um-2 user, am-2 assistant]."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(store.save_session(
        ChatSessionRecord(
            id=session_id,
            title="t",
            title_source="auto",
            agent_name="default",
            owner_user_id=None,
            workspace_id=None,
            preview_text="",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            memory_messages=[],
            messages=[
                ChatTranscriptMessage(id="um-1", role="user", content="first"),
                ChatTranscriptMessage(id="am-1", role="assistant", content="reply 1"),
                ChatTranscriptMessage(id="um-2", role="user", content="second"),
                ChatTranscriptMessage(id="am-2", role="assistant", content="reply 2"),
            ],
            activity=[],
        )
    ))


class TranscriptReplaceRouteTestCase(unittest.TestCase):
    def test_truncate_drops_message_and_tail(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "um-2"})

        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.json()["messages"]]
        self.assertEqual(ids, ["um-1", "am-1"])

    def test_truncate_unknown_message_id_404(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "nope"})

        self.assertEqual(resp.status_code, 404)

    def test_explicit_replace_restores_tail(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript", json={
            "messages": [
                {"id": "um-1", "role": "user", "content": "first"},
                {"id": "am-1", "role": "assistant", "content": "reply 1"},
                {"id": "um-2", "role": "user", "content": "second"},
                {"id": "am-2", "role": "assistant", "content": "reply 2"},
            ],
        })

        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.json()["messages"]]
        self.assertEqual(ids, ["um-1", "am-1", "um-2", "am-2"])

    def test_explicit_replace_rejects_empty(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript", json={"messages": []})
        self.assertEqual(resp.status_code, 400)

    def test_neither_field_supplied_400(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript", json={})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_session_404(self) -> None:
        store = InMemorySessionStore()
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/missing/transcript",
                          json={"truncate_before_message_id": "um-1"})
        self.assertEqual(resp.status_code, 404)
```

Note on the "owner check rejects other user" case: the shared `_ensure_console_principal_can_access_session` is already covered by existing session-route tests, and the admin fake-DB makes every request an admin (so a non-admin path needs a bespoke fake). That path is out of scope for this task's unit tests — the integration owner-check coverage is not regressed because we reuse the existing helper unchanged. Do not add a bespoke non-admin fake for v1.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py::TranscriptReplaceRouteTestCase -v`
Expected: FAIL — `404`/`405` (route does not exist yet) on every case.

- [ ] **Step 3: Write minimal implementation**

(a) Add imports to the top import block of `src/covalent/api/routes/sessions.py` (group with existing imports):

```python
from covalent.application.schemas import TranscriptReplaceRequest
from covalent.application.services.session_service import _build_session_preview
from covalent.infra.memory import ChatSessionRecord
from covalent.infra.memory import ChatTranscriptMessage
```

(b) Add the route after the `rename_session` function (after its `return`, before `delete_session`). Mirror the structure of `rename_session` (resolve principal, get session, owner check, save, return response):

```python
@router.put("/sessions/{session_id}/transcript")
async def replace_transcript(
    request: Request, session_id: str, update_request: TranscriptReplaceRequest
) -> ChatSessionResponse:
    session_store: SessionStore = request.app.state.session_store
    principal = await _resolve_console_principal(request, request.app.state.db_manager)

    existing = await session_store.get_session(session_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
    _ensure_console_principal_can_access_session(principal, existing)

    # Decide the new message list. Activity is intentionally left unchanged
    # (append-only store; stale trace rows are harmless in the flat history).
    if update_request.truncate_before_message_id is not None:
        target_id = update_request.truncate_before_message_id
        try:
            index = next(
                i for i, m in enumerate(existing.messages) if m.id == target_id
            )
        except StopIteration:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown message id: {target_id}",
            ) from None
        new_messages = existing.messages[:index]
    elif update_request.messages is not None:
        if not update_request.messages:
            raise HTTPException(status_code=400, detail="messages must not be empty")
        ids = [m.id for m in update_request.messages]
        if len(set(ids)) != len(ids):
            raise HTTPException(status_code=400, detail="message ids must be unique")
        new_messages = [
            ChatTranscriptMessage(
                id=m.id, role=m.role, content=m.content, attachments=list(m.attachments)
            )
            for m in update_request.messages
        ]
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either truncate_before_message_id or messages.",
        )

    record = ChatSessionRecord(
        id=existing.id,
        title=existing.title,
        title_source=existing.title_source,
        agent_name=existing.agent_name,
        owner_user_id=existing.owner_user_id,
        workspace_id=existing.workspace_id,
        created_by_token_id=existing.created_by_token_id,
        preview_text=_build_session_preview(new_messages),
        created_at=existing.created_at,
        updated_at=datetime.now(UTC),
        memory_messages=existing.memory_messages,
        messages=new_messages,
        activity=existing.activity,
    )
    saved = await session_store.save_session(record)
    return to_chat_session_response(saved)
```

`datetime` and `UTC` are already imported at the top of `sessions.py`. `ChatSessionResponse`, `to_chat_session_response`, `_resolve_console_principal`, `_ensure_console_principal_can_access_session`, `SessionStore`, `HTTPException`, `Request` are all already imported in this file (confirmed in the import block). Verify each before saving.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py -v`
Expected: PASS — truncate drops message+tail, 404 on unknown id, explicit-replace restores, owner check rejects.

- [ ] **Step 5: Commit**

```bash
git add src/covalent/api/routes/sessions.py tests/test_session_transcript_replace_api.py
git commit -m "feat: add PUT /sessions/{id}/transcript endpoint for edit-and-resend"
```

---

## Task 3: Backend store-level round-trip (truncate persists; preserved fields survive)

**Files:**
- Modify: `tests/test_session_transcript_replace_api.py` (add a store-level round-trip test)

**Why store-level, not full HTTP stream:** the transcript (`messages` with stable ids) is assembled and persisted only in the stream *route* (`agents.py`), which loads `session.messages`, appends one turn, and re-saves. That route path already has its own coverage; re-testing the full stream here would require wiring registry+agent+fake model into `app.state` and would mainly re-exercise unchanged code. The highest-value *new* assertion is the store contract the route depends on: after the endpoint truncates, a fresh `get_session` reflects the truncation, and `memory_messages` / title / agent / timestamps survive a replace. This task verifies that contract directly against `InMemorySessionStore` (and `PersistentSessionStore` is already covered by `tests/test_persistent_session_store.py`, which exercises the same `save_session` DELETE+re-insert path).

**Interfaces:**
- Consumes: Task 2 endpoint behavior via the route; `InMemorySessionStore.get_session` / `save_session`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_session_transcript_replace_api.py`:

```python
class TranscriptReplaceStoreRoundTripTestCase(unittest.TestCase):
    def test_truncate_then_reload_reflects_truncation(self) -> None:
        store = InMemorySessionStore()
        _seed_session(store, "sess-1")
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-1/transcript",
                          json={"truncate_before_message_id": "um-2"})
        self.assertEqual(resp.status_code, 200)

        # The store itself must reflect the truncation (this is what the stream
        # route will load on the next turn).
        import asyncio
        reloaded = asyncio.get_event_loop().run_until_complete(store.get_session("sess-1"))
        self.assertIsNotNone(reloaded)
        ids = [m.id for m in reloaded.messages]
        self.assertEqual(ids, ["um-1", "am-1"])

    def test_replace_preserves_title_agent_and_memory(self) -> None:
        store = InMemorySessionStore()
        import asyncio
        # Seed with a non-default title and a memory message so we can assert
        # they survive the replace.
        asyncio.get_event_loop().run_until_complete(store.save_session(
            ChatSessionRecord(
                id="sess-2", title="Important", title_source="manual",
                agent_name="researcher", owner_user_id=None, workspace_id=None,
                preview_text="old preview", created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                memory_messages=[],  # memory is loaded separately; just assert it stays empty
                messages=[
                    ChatTranscriptMessage(id="um-1", role="user", content="hello"),
                ],
                activity=[],
            )
        ))
        client = _build_app_with_store(store)

        resp = client.put("/api/backend/sessions/sess-2/transcript", json={
            "messages": [{"id": "um-1", "role": "user", "content": "hello edited"}],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["title"], "Important")
        self.assertEqual(body["title_source"], "manual")
        self.assertEqual(body["agent_name"], "researcher")
        self.assertEqual(body["messages"][0]["content"], "hello edited")
        # preview is recomputed from the new message list, not preserved verbatim
        self.assertIn("hello edited", body["preview_text"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py::TranscriptReplaceStoreRoundTripTestCase -v`
Expected: FAIL until Task 2's route is implemented (these tests live in the same file and run after Task 2's commit, so if run in isolation before Task 2 they 404).

- [ ] **Step 3: (no production change expected)**

These tests exercise the Task 2 endpoint + store. If they fail after Task 2 is in, the bug is in Task 2's truncation or field-preservation, not here.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py -v`
Expected: PASS for all classes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_session_transcript_replace_api.py
git commit -m "test: add store-level round-trip for transcript replace"
```

---

## Task 4: Frontend client API + type

**Files:**
- Modify: `frontend/lib/client-api.ts` (add `replaceChatTranscript`; add `ChatTranscriptMessageInput` type near the top with the other local types or import from `types.ts`)

**Interfaces:**
- Consumes: the backend endpoint from Task 2.
- Produces: `replaceChatTranscript(sessionId, body) -> Promise<ChatSession>` and `ChatTranscriptMessageInput`, consumed by Task 6.

- [ ] **Step 1: Write the failing test (type-level)**

There is no frontend test harness in the repo (confirmed: no `*.test.ts`/vitest/jest in `frontend/`). So the "test" is a typed usage that must compile. Add to `frontend/lib/client-api.ts` a temporary type-check assertion at the bottom (delete after the function exists), or simply rely on the TypeScript compiler. Preferred: add the real function in Step 3 and let `tsc` validate.

Run to confirm current state compiles: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit`
Expected: PASS (nothing changed yet).

- [ ] **Step 2: (no separate failing-test step for frontend)**

Skip — frontend has no unit-test harness; verification is `tsc --noEmit` + manual.

- [ ] **Step 3: Write minimal implementation**

In `frontend/lib/client-api.ts`, add the type near the other types/imports (e.g. after the imports block at the top, before `const API_PREFIX`):

```ts
export type ChatTranscriptMessageInput = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: unknown[];
};
```

Add the function next to the other session functions (after `deleteChatSession`, ~L173):

```ts
export type TranscriptReplaceBody = {
  truncate_before_message_id?: string;
  messages?: ChatTranscriptMessageInput[];
};

export function replaceChatTranscript(sessionId: string, body: TranscriptReplaceBody): Promise<ChatSession> {
  return apiFetchJson<ChatSession>(`sessions/${encodeURIComponent(sessionId)}/transcript`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
```

`apiFetchJson` and `ChatSession` are already imported in this file (the file imports `ChatSession` in its type import list — confirm; if not, add it to the existing `import type { ... } from "@/lib/types"` block).

- [ ] **Step 4: Run type-check to verify it passes**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit`
Expected: PASS, no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/client-api.ts
git commit -m "feat: add replaceChatTranscript client API"
```

---

## Task 5: Frontend — read the Next.js docs gate

**Files:** none (research gate per `frontend/AGENTS.md`)

- [ ] **Step 1: List available Next.js docs**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && ls node_modules/next/dist/docs/ 2>/dev/null || echo "no docs dir"`

- [ ] **Step 2: Read any doc relevant to client components / event handlers / forms**

If a docs dir exists, read the guide covering client components and event handlers (`"use client"`, `onChange`, `onSubmit`). This feature adds a `<textarea>` with `onChange`/`onKeyDown` inside an existing `"use client"` component (`chat-workspace.tsx` already starts with `"use client"`), so confirm no breaking-change trap. If no docs dir, note that and proceed.

No commit — this is a verification step.

---

## Task 6: Frontend — inline edit UI on user bubbles

**Files:**
- Modify: `frontend/components/chat-workspace.tsx`:
  - `ChatMessageBubble` (~L624-666): add an "Edit" affordance for user messages + inline textarea mode.
  - main component state: add `editingMessageId` + `editingDraft` state.
  - pass `sending`, `editingMessageId`, `editingDraft`, and handlers down to `ChatMessageBubble`.

**Interfaces:**
- Consumes: `Message` type (has `id`, `role`, `content`, `attachments?`); the main component's `sending` flag; `ComposerAttachment` type.
- Produces: inline edit UI; this task wires the buttons but the resend handler is stubbed to call the real handler added in Task 7. To keep Task 6 independently testable (compiles + renders), the resend handler is passed in as a prop from the main component (defined in Task 7). For Task 6, define a no-op default and verify the UI renders.

- [ ] **Step 1: Read the current `ChatMessageBubble` + how it's called**

Read `frontend/components/chat-workspace.tsx:624-666` (the bubble) and find its call site (search `ChatMessageBubble`).

- [ ] **Step 2: Add edit state to the main component**

Near the other `useState` hooks (e.g. next to `const [sending, setSending] = useState(false);` ~L1821), add:

```tsx
const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
const [editingDraft, setEditingDraft] = useState("");
```

- [ ] **Step 3: Render the Edit control on user bubbles**

Modify `ChatMessageBubble` signature to accept the edit props:

```tsx
function ChatMessageBubble({
  message,
  sending,
  editingMessageId,
  editingDraft,
  onEditStart,
  onEditChange,
  onEditCancel,
  onEditSubmit,
}: {
  message: Message;
  sending: boolean;
  editingMessageId: string | null;
  editingDraft: string;
  onEditStart: (message: Message) => void;
  onEditChange: (value: string) => void;
  onEditCancel: () => void;
  onEditSubmit: () => void;
}) {
  const isEditing = editingMessageId === message.id;
  const canEdit = message.role === "user" && !sending && !isEditing && !message.askUserPrompt;
```

In the JSX:
- When `message.role === "user"` and not editing and not `sending`, render a hover "Edit" button (styled like `ChatBubbleCopy`'s button — reuse the same class pattern, e.g. `className="chat-bubble-copy"` with a pencil glyph or the text "Edit"). Wire `onClick={() => onEditStart(message)}`.
- When `isEditing`, replace the `<ChatMarkdownContent>` body with:

```tsx
<div className="chat-bubble-edit">
  <textarea
    className="chat-bubble-edit-input"
    value={editingDraft}
    onChange={(event) => onEditChange(event.target.value)}
    rows={3}
  />
  <div className="chat-bubble-edit-actions">
    <button type="button" onClick={onEditCancel}>Cancel</button>
    <button type="button" onClick={onEditSubmit} disabled={!editingDraft.trim()}>
      Save &amp; resend
    </button>
  </div>
</div>
```

- [ ] **Step 4: Wire the call site**

At the `ChatMessageBubble` call site, pass:

```tsx
<ChatMessageBubble
  message={message}
  sending={sending}
  editingMessageId={editingMessageId}
  editingDraft={editingDraft}
  onEditStart={(m) => { setEditingMessageId(m.id); setEditingDraft(m.content); }}
  onEditChange={setEditingDraft}
  onEditCancel={() => { setEditingMessageId(null); setEditingDraft(""); }}
  onEditSubmit={() => { /* Task 7 wires this to handleEditResend */ }}
/>
```

- [ ] **Step 5: Add minimal CSS**

Add to the existing `<style>` block in `chat-workspace.tsx` (search for `chat-bubble-copy` styles and add adjacent rules):

```css
.chat-bubble-edit { display: flex; flex-direction: column; gap: 0.5rem; width: 100%; }
.chat-bubble-edit-input { width: 100%; resize: vertical; min-height: 4rem; }
.chat-bubble-edit-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
```

- [ ] **Step 6: Verify it compiles**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 7: Manual check**

Run the dev server per the project's `run` skill / `dev.sh`, open the chat, send a message, hover the user bubble: Edit button appears; clicking turns the bubble into a textarea pre-filled with the content; Cancel restores. Save does nothing yet (Task 7).

- [ ] **Step 8: Commit**

```bash
git add frontend/components/chat-workspace.tsx
git commit -m "feat(chat): inline edit UI for user messages"
```

---

## Task 7: Frontend — `handleEditResend` (truncate + regenerate)

**Files:**
- Modify: `frontend/components/chat-workspace.tsx` (add `handleEditResend`; wire `onEditSubmit`)

**Interfaces:**
- Consumes: `replaceChatTranscript` (Task 4); `runThreadRequest` (existing, ~L2247); `buildRequestInput` (existing, L734); `useChatSessions().updateThread` + `activeThread`.
- Produces: working edit-and-resend.

- [ ] **Step 1: Write the handler**

Add `handleEditResend` near `handleSend` (~L2469). It mirrors `runThreadRequest`'s prelude but truncates first:

```tsx
async function handleEditResend(message: Message, newContent: string) {
  if (!currentAgent || sending || !activeThread || !newContent.trim()) {
    return;
  }

  const thread = activeThread;
  const messageIndex = thread.messages.findIndex((m) => m.id === message.id);
  if (messageIndex === -1) {
    return;
  }

  // Snapshot for undo: original message + everything strictly after it.
  const originalMessage = message;
  const removedTail = thread.messages.slice(messageIndex + 1);
  const prefix = thread.messages.slice(0, messageIndex);
  // Stash the snapshot in the per-thread undo slot (Task 8 reads this).
  editUndoRef.current = {
    threadId: thread.id,
    sessionId: thread.sessionId,
    originalMessage,
    removedTail,
    prefix,
  };

  setEditingMessageId(null);
  setEditingDraft("");

  // 1. Truncate server-side transcript (skip if the thread isn't persisted yet).
  if (thread.isPersisted) {
    try {
      await replaceChatTranscript(thread.sessionId, {
        truncate_before_message_id: message.id,
      });
    } catch (truncateError) {
      setError(truncateError instanceof Error ? truncateError.message : "Failed to edit message.");
      editUndoRef.current = null;
      return;
    }
  }

  // 2. Drop message-and-after from the local thread.
  updateThread(thread.id, (t) => ({
    ...t,
    updatedAt: Date.now(),
    messages: t.messages.slice(0, messageIndex),
  }));

  // 3. Rebuild the edited input and resend via the existing run loop.
  const attachments = (message.attachments ?? []) as ComposerAttachment[];
  const requestInput = buildRequestInput(newContent, attachments);
  await runThreadRequest({
    thread,
    requestInput,
    userContent: newContent,
    attachments,
    clearPendingQuestion: true,
  });

  // 4. Offer undo (Task 8 surfaces the toast; here we just flag completion).
  showEditUndoToast(thread.id, newContent);
}
```

Also declare the ref at the top of the component (near `activeRunRef`):

```tsx
const editUndoRef = useRef<{
  threadId: string;
  sessionId: string;
  originalMessage: Message;
  removedTail: Message[];
  prefix: Message[];
} | null>(null);
```

`showEditUndoToast` is added in Task 8; for Task 7 define it as a local no-op stub so the file compiles, then Task 8 replaces it.

- [ ] **Step 2: Wire `onEditSubmit`**

At the `ChatMessageBubble` call site, replace the Task 6 placeholder:

```tsx
onEditSubmit={() => {
  if (editingMessageId) {
    const msg = activeThread?.messages.find((m) => m.id === editingMessageId);
    if (msg) {
      void handleEditResend(msg, editingDraft);
    }
  }
}}
```

- [ ] **Step 3: Verify it compiles**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 4: Manual check**

In the running app: send 2+ turns, hover an earlier user message, edit it, Save & resend. The later messages disappear, a new turn streams in. Confirm the discarded tail is gone from both the UI and (reload the page) the server.

- [ ] **Step 5: Commit**

```bash
git add frontend/components/chat-workspace.tsx
git commit -m "feat(chat): edit user message truncates transcript and regenerates"
```

---

## Task 8: Frontend — Undo via sonner toast

**Files:**
- Modify: `frontend/components/chat-workspace.tsx` (add `showEditUndoToast` + `handleUndoEdit`; import `toast` from `sonner`)

**Interfaces:**
- Consumes: `editUndoRef` (Task 7); `replaceChatTranscript` with explicit `messages` (Task 4); `updateThread`.
- Produces: working Undo.

- [ ] **Step 1: Add the sonner import**

At the top of `chat-workspace.tsx` with the other imports:

```tsx
import { toast } from "sonner";
```

(Confirm sonner is a dependency — it is, via `frontend/components/ui/sonner.tsx` and `skill-file-preview.tsx` already importing it.)

- [ ] **Step 2: Implement `showEditUndoToast` and `handleUndoEdit`**

Replace the Task 7 stub `showEditUndoToast` with:

```tsx
function showEditUndoToast(threadId: string, editedContent: string) {
  toast("Edited message and resent.", {
    action: {
      label: "Undo",
      onClick: () => { void handleUndoEdit(); },
    },
    duration: 8000,
  });
}

async function handleUndoEdit() {
  const snapshot = editUndoRef.current;
  if (!snapshot) {
    return;
  }
  const { sessionId, originalMessage, removedTail, prefix } = snapshot;
  const restored = [...prefix, originalMessage, ...removedTail];
  try {
    await replaceChatTranscript(sessionId, {
      messages: restored.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        attachments: (m.attachments ?? []) as unknown[],
      })),
    });
  } catch (undoError) {
    setError(undoError instanceof Error ? undoError.message : "Failed to undo edit.");
    return;
  }
  updateThread(snapshot.threadId, (t) => ({ ...t, messages: restored, updatedAt: Date.now() }));
  editUndoRef.current = null;
}
```

`showEditUndoToast` and `handleUndoEdit` are defined inside the component (they close over `editUndoRef`, `updateThread`, `setError`) — place them as component-internal functions, not module-level. (Note: defining a component-internal function named in a toast callback is fine; ensure `showEditUndoToast` is declared before `handleEditResend` uses it, or hoist via `function` declaration which is fine inside a component body.)

- [ ] **Step 3: Clear the undo slot when a newer run/edit starts**

In `handleEditResend` (Task 7) and in `runThreadRequest`'s start, the slot is already overwritten on each new edit. Additionally, clear it in `handleNewChat` and on thread switch by adding `editUndoRef.current = null;` at the top of `handleNewChat`. (Read `handleNewChat` first; add the clear as its first statement.)

- [ ] **Step 4: Verify it compiles**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Manual check**

Edit-and-resend a message, then click "Undo" in the toast within 8s: the discarded tail returns, the edited message reverts to its original content. No new generation runs. Reload the page: the restored transcript is persisted.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/chat-workspace.tsx
git commit -m "feat(chat): add undo for message edit via toast"
```

---

## Task 9: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full backend test run**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest tests/test_session_transcript_replace_api.py tests/test_session_transcript.py tests/test_persistent_session_store.py -v`
Expected: all PASS.

- [ ] **Step 2: Full backend suite (sanity — no regressions)**

Run: `cd /Users/chijiangduan/projs/Covalent && .venv/bin/python -m pytest -q`
Expected: PASS (no new failures vs. baseline).

- [ ] **Step 3: Frontend type-check + build**

Run: `cd /Users/chijiangduan/projs/Covalent/frontend && pnpm exec tsc --noEmit && pnpm build`
Expected: PASS.

- [ ] **Step 4: End-to-end manual smoke test**

In the running app: send a multi-turn conversation; edit an earlier user message (with and without attachments); confirm truncate + regenerate; confirm undo restores; reload and confirm persistence; confirm Edit is disabled while a run is streaming.

No commit (verification only). Hand the finished branch to the user.
