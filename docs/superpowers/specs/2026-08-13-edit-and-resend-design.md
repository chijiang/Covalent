# Edit-and-resend for user messages — Design

Date: 2026-08-13
Status: Draft (awaiting user review)

## Goal

Add ChatGPT/Gemini-style **edit-and-resend** to the chat workspace: hover a user
message, click Edit, the bubble becomes an inline editable textarea; on save,
everything after that message is discarded and the assistant regenerates from the
edited text. A transient toast offers Undo to restore the discarded tail.

## Scope

**In scope**
- Editing a user message's text in place, preserving its attachments.
- Discarding all messages after the edited message (discard + undo).
- Regenerating the assistant turn from the edited text.
- Client-side Undo that restores the discarded tail.

**Out of scope**
- Branching / version switching (no fork model).
- Regenerating arbitrary assistant replies.
- Editing assistant messages.
- Editing while a run is streaming (`sending` is true).
- The public invoke API and API-token runs (console only).
- Trimming stored activity/trace rows on edit (left as-is; see Boundary).

## Background / architecture constraint

The backend rebuilds every turn from server-stored session messages. The stream
route (`src/covalent/api/routes/agents.py`, `POST /agents/{name}/stream`) loads
`session_store.get_session(session_id).messages`, appends the new user message,
runs the agent, and re-persists the whole transcript in `finally`. The client
never sends conversation history — only `session_id` + new input.

Messages are a **flat linear list with stable ids** (`ChatTranscriptMessage`,
`src/covalent/infra/memory.py`). There is no branching model and no existing
endpoint to mutate stored messages — only list / get / rename-title /
delete-whole-session.

Consequence: edit-and-resend fundamentally means **truncate the server-side
transcript to a chosen point, then re-run via the existing stream.** This design
adds exactly one backend endpoint to perform that truncation (and its inverse,
restore), and reuses the stream unchanged.

## End-to-end flow

```
1. User clicks Edit on user message M (id=m, thread T, session S)
   → bubble becomes inline <textarea> pre-filled with M.content.

2. User edits text, clicks "Save & resend"
   → client snapshots for undo:
       originalMessage = deep copy of M (its pre-edit content + attachments)
       removedTail     = deep copy of thread.messages strictly after M
       prefix          = thread.messages strictly before M

3. Client: PUT /sessions/S/transcript { truncate_before_message_id: m }
   → backend deletes M and everything after it (keeps the strict prefix)
   → recomputes preview_text from the surviving prefix
   → returns the truncated ChatSession
   → client drops M-and-after from the local thread.

4. Client: POST /agents/{agent}/stream
   input = buildRequestInput(editedText, M.attachments)
   session_id = S
   → backend loads session.messages (the truncated prefix),
     appends the edited user message, regenerates the assistant
   → streams as normal; persists the new turn on completion.
```

**Undo** (transient toast right after an edit completes):
```
Client stores, in component state (one slot per thread):
  { originalMessage, removedTail, prefix }   // all captured before truncation

handleUndoEdit:
  → PUT /sessions/S/transcript { messages: prefix + originalMessage + removedTail }
     (full explicit replace — restores the exact pre-edit transcript, i.e. the
     user message reverts to its original content and the discarded tail returns)
  → updateThread to re-render the restored bubbles
  → no regeneration
```

### Notes on the flow

- **Two requests per edit** (PUT then stream). If PUT succeeds but the stream
  fails, the session is simply shorter than before; the undo snapshot is still
  valid, so undo restores the tail. Recoverable, no data loss.
- **Undo is client-state only** — the snapshot lives in a ref keyed by thread,
  cleared on navigation or reload. Reloading after an edit loses undo; the
  truncated transcript is then the source of truth. This delivers "discard +
  undo" without server-side version history.

## Backend

### New endpoint

`src/covalent/api/routes/sessions.py`, alongside the existing
`PATCH /sessions/{id}` (rename) route:

```python
@router.put("/sessions/{session_id}/transcript")
async def replace_transcript(
    request: Request, session_id: str, body: TranscriptReplaceRequest
) -> ChatSessionResponse:
```

### Request / response

`TranscriptReplaceRequest` — defined in `src/covalent/application/schemas.py`
next to the other chat schemas:

```python
class TranscriptReplaceRequest(BaseModel):
    # Mutually exclusive — exactly one required.
    truncate_before_message_id: str | None = None  # keep messages strictly before this id
    messages: list[ChatTranscriptMessageInput] | None = None  # explicit full replace (undo)
```

`ChatTranscriptMessageInput` mirrors `ChatTranscriptMessage`:
`{ id: str, role: "user"|"assistant", content: str, attachments: list[dict] }`.

Response: the same `ChatSessionResponse` shape returned by `GET /sessions/{id}`,
so the frontend's existing `ChatSession` type is reused unchanged.

### Behavior

- `existing = await session_store.get_session(session_id)`; 404 if missing.
- Apply the existing `_ensure_console_principal_can_access_session` owner check
  (same as the other session routes).
- If `truncate_before_message_id`: find its index in `existing.messages`; new
  message list = `existing.messages[:index]`; 404 if the id is not present.
- If `messages`: validate unique ids and non-empty; otherwise full replace.
- `await session_store.save_session(ChatSessionRecord(...))` with:
  - `messages` = new list
  - `preview_text` = recomputed via the existing `_build_session_preview` helper
    (currently in `agents.py`; lift to a shared location or import).
  - `memory_messages`, `activity`, `title`, `title_source`, `agent_name`,
    `owner_user_id`, `workspace_id`, timestamps = preserved from `existing`.
- Return `ChatSessionResponse` from the saved record.

### Why no migration

`PersistentSessionStore.save_session` already performs
`DELETE FROM chat_messages WHERE session_id = ?` then re-inserts the supplied
messages (`memory.py` ~L225-238). A full transcript replace is therefore native
and atomic. No schema change, no alembic migration.

### Boundary (v1)

`ChatActivityRow` inserts are append-only (`on_conflict_do_nothing`,
`memory.py` ~L241-257). On edit, messages are truncated but **old activity /
trace rows are left as-is** — the activity panel renders them as a flat history
unattached to individual messages, so stale tool-trace entries from discarded
turns simply remain. The route will carry a one-line comment noting activity is
preserved, not trimmed. Trimming activity by cutoff is a possible later
additive change (a `DELETE` on `ChatActivityRow`) and is not needed for v1.

### Untouched

`agents.py`, `AgentRunRequest`, the public invoke API, and the API-token runs.
The stream handler is reused verbatim.

## Frontend

All changes in `frontend/components/chat-workspace.tsx` and
`frontend/lib/client-api.ts`. Per `frontend/AGENTS.md`, read the relevant
`node_modules/next/dist/docs/` guide before writing any Next.js code; this
feature is plain React state + fetch (no routing / App-Router conventions), so
risk is low.

### `client-api.ts` — one new function

```ts
export function replaceChatTranscript(
  sessionId: string,
  body: { truncate_before_message_id?: string; messages?: ChatTranscriptMessageInput[] },
): Promise<ChatSession> {
  return apiFetchJson<ChatSession>(`sessions/${encodeURIComponent(sessionId)}/transcript`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}
```

Plus a `ChatTranscriptMessageInput` type (`{ id, role, content, attachments }`)
matching `ChatTranscriptMessage`.

### `chat-workspace.tsx`

**`ChatMessageBubble` — edit affordance (user messages only):**
- Hover-revealed "Edit" button on outbound bubbles, styled like the existing
  `ChatBubbleCopy` hover control (no new visual pattern).
- Hidden while `sending`, and while an edit is already in progress on this
  thread.
- Click sets local state `editingMessageId`; the bubble's markdown body is
  replaced by a `<textarea>` pre-filled with `message.content`, with
  "Save & resend" and "Cancel" buttons.

**New handler `handleEditResend(message, newContent)`:**
```
1. snapshot removedTail = deep copy of thread.messages strictly after `message`
2. if thread.isPersisted:
     await replaceChatTranscript(sessionId, { truncate_before_message_id: message.id })
3. updateThread: drop message-and-after from local messages
4. requestInput = buildRequestInput(newContent, message.attachments)
5. runThreadRequest({ thread, requestInput, userContent: newContent,
                      attachments: message.attachments, clearPendingQuestion: true })
   // runThreadRequest already appends the new user msg + assistant placeholder
   // and streams; reused unchanged.
```

Reuses the existing `runThreadRequest` unchanged — the edited message is treated
as a fresh send into the now-truncated thread.

**Undo — one transient slot per thread (toast UI):**
- After step 5 completes, store `{ originalMessage, removedTail, prefix }` in a
  ref keyed by thread id, and surface an "Undo edit" **toast**. The toast is dismissed by
  timeout, by clicking Undo, or when a newer edit/run begins on the thread.
- `handleUndoEdit`: PUT `replaceChatTranscript(sessionId, { messages: prefix +
  originalMessage + removedTail })`; `updateThread` to restore bubbles;
  no regeneration.

**Attachments on edited messages:** the textarea edits text only;
`message.attachments` are carried into `buildRequestInput` unchanged. The edit
UI shows a read-only note listing preserved attachments.

### Edge cases

- Editing while `sending` → Edit button disabled.
- Editing on a non-persisted draft thread (`!isPersisted`) → nothing server-side
  to truncate; skip the PUT (step 2) and rebuild locally only.
- Stream fails after a successful PUT → truncated thread stays; undo snapshot
  still valid.
- Page reload after edit → undo slot gone (client-state only); truncated
  transcript is truth.

## Testing

**Backend** (`tests/`, mirroring existing session-route tests):
- `PUT /sessions/{id}/transcript` with `truncate_before_message_id` → returns
  correctly truncated messages + recomputed preview; 404 on unknown message id;
  owner check rejects other users' sessions.
- Explicit `messages` replace (undo path) → full overwrite returned; rejects
  empty / duplicate ids.
- Both stores: `InMemorySessionStore` for unit speed; `PersistentSessionStore`
  already exercises the DELETE + re-insert path in existing tests.
- Round-trip: truncate then stream → new turn appends to the truncated
  transcript and persists correctly.

**Frontend** (manual + type-level; no frontend test harness in the repo):
- Edit button appears only on user messages and hides while `sending`.
- Textarea pre-fills; Cancel restores the original.
- Save truncates then streams; undo restores the tail.
- Attachments preserved through the round trip.

No DB migration → no migration test.

## Open questions for review

None — all design decisions confirmed during brainstorming:
- Action: edit user message and resend.
- History after edit: discard + undo.
- Edit interaction: inline editing.
- Backend approach: A (transcript endpoint).
- Attachments: preserve originals.
- Undo UI: toast.
