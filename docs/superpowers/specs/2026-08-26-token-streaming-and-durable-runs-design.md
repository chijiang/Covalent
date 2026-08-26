# Token-level streaming, user cancellation, and background runs — Design

**Date:** 2026-08-26

**Status:** Draft

**Scope:** Make the final answer of every conversation turn stream at token
granularity, let the user interrupt an in-flight answer without corrupting the
session, and let inference continue in the background when the user navigates
away, with full event replay on return.

## Summary

Today the console chat pipeline couples three lifecycles that should be
independent: the SSE connection, the ReAct execution, and the session write.

- `ReactAgentRuntime._run_stream` (`src/covalent/runtime/react.py:1513`) drives
  **every** model call through the non-streaming `adapter.generate()`, so the
  `assistant` SSE event carries the complete answer text in one shot.
- `POST /agents/{name}/stream` (`src/covalent/api/routes/agents.py:134`) runs
  the whole ReAct loop *inside* the SSE response generator. When the client
  disconnects, the generator is cancelled and the run dies mid-flight.
- The frontend (`frontend/components/chat-workspace.tsx:2507`) aborts the fetch
  to "stop" a run — which is exactly the same mechanism as navigating away, so
  "user cancelled" and "user left the page" are indistinguishable and both
  kill the inference.

This design separates them:

1. **Token deltas.** The runtime adds a streaming generation path that consumes
   `adapter.stream()` chunk-by-chunk, emits `assistant_delta` SSE events per
   text delta, and reassembles the same `GenerationResponse` the loop already
   consumes. Non-final iterations (tool-calling rounds) keep using
   `generate()`.
2. **Durable runs.** A new `chat_runs` table records each turn. The route
   creates the run, spawns a background asyncio task that executes the ReAct
   loop, publishes every event into an in-process broadcast bus plus a
   `chat_run_events` replay log, and returns a `run_id`. SSE becomes a
   *view* over the run: `GET /agents/{name}/runs/{run_id}/events` replays
   persisted events from `Last-Event-ID` and then tails the bus. Disconnecting
   the view never touches execution.
3. **Cancellation.** `POST /agents/{name}/runs/{run_id}/cancel` flips run
   status to `cancelling`; the worker observes it (between events, and by
   cancelling the in-flight model stream) and terminates the run as
   `cancelled`, persisting whatever partial output exists. The frontend Stop
   button calls this API instead of aborting the fetch.

## Goals

1. Final answers stream at token granularity with no behavioral change to
   model memory, tool execution, or the `final` event contract.
2. A run survives SSE disconnects (page navigation, refresh, tab hide) and
   replays its events losslessly on reconnect.
3. Users can stop a generating answer; the partial answer is kept and the
   session remains resumable.
4. The legacy `POST /agents/{name}/stream` endpoint remains functional for
   existing API consumers, implemented on top of the same run machinery.
5. No new infrastructure: no Celery, no Redis. In-process asyncio worker +
   PostgreSQL event log.

## Non-goals

- Cross-process or multi-replica execution (single-instance deployment
  assumption; the event bus is in-process, the replay log is in Postgres so a
  restart surfaces runs as `failed`).
- Streaming tool-call argument deltas or reasoning deltas.
- Changing delegate (subagent) streaming behavior beyond inheriting delta
  events where already forwarded.
- Changing the public invoke (non-console) API shape.

## Current architecture (what exists)

```
POST /agents/{name}/stream          (agents.py:134)
  └─ event_stream() generator       — runs INSIDE the SSE response
      └─ AgentInvocationService.stream (agent_invocation.py:62)
          └─ ReactAgentRuntime.stream_events → _run_stream (react.py:1513)
              └─ adapter.generate()           — non-streaming, every iteration
      └─ finally: save ChatSessionRecord (transcript + activity + memory)
```

Frontend: `streamAgent()` (`frontend/lib/client-api.ts:377`) reads the SSE body
via fetch + ReadableStream; `chat-workspace.tsx` aborts via `AbortController`
on unmount/new-run/stop — all three cases kill the backend run.

Session persistence: three datasets per session (transcript `chat_messages`,
trace `chat_activity`, model memory in `chat_sessions.memory_messages`),
written once in the route's `finally` block. Analysis in
`docs/agent-stream-history-and-hitl.md`.

## Proposed design

### 1. Streaming generation in the runtime

`ModelAdapter.stream()` already yields raw OpenAI-compatible chunk JSON
(`src/covalent/model/openai_compatible.py:88`). The runtime gains a
`_generate_streaming` helper used for the **final-answer model call** — i.e.
the call made when the model is about to answer without tools. Since we cannot
know in advance whether a call will produce tool calls, the practical rule is:

- All model calls in the root run use the streaming path.
- Text deltas are emitted as `assistant_delta` events **only while the model
  has not yet opened a tool call** in the current response. If the aggregated
  response turns out to contain tool calls, the deltas already emitted are
  superseded by the existing `assistant` event (which today already replaces
  the transcript text — `_upsert_assistant_transcript`), so the contract stays
  safe.

`_generate_streaming(request)` returns `(response, delta_iterator)`: it
aggregates chunks into the same `GenerationResponse` shape `generate()`
produces (content parts, tool_calls with incremental argument concatenation,
usage from the final chunk) while yielding each `choices[0].delta.content`
fragment to the caller as it arrives. The ReAct loop then:

```python
async for delta in deltas:
    yield {"event": "assistant_delta", "payload": {"text": delta, "iteration": iteration}}
# after stream ends, proceed exactly as today with the aggregated `response`
```

The `assistant` (full text) and `final` events remain unchanged — deltas are
additive. Consumers that ignore `assistant_delta` lose nothing.

Fallback: if the provider lacks `Capability.STREAMING`, the loop calls
`generate()` and emits one synthetic delta containing the whole text.

### 2. Durable runs

New tables (alembic migration `20260826_000028_create_chat_runs.py`):

```sql
chat_runs (
  id            text PK,                  -- 'run_<ulid>'
  session_id    text NOT NULL FK chat_sessions(id) ON DELETE CASCADE,
  agent_name    text NOT NULL,
  owner_user_id text NULL FK users(id),
  workspace_id  text NULL,
  status        text NOT NULL,            -- running|completed|cancelling|cancelled|failed
  error_json    jsonb NOT NULL DEFAULT '{}',
  input_json    jsonb NOT NULL,           -- AgentRunInput (input + metadata), for restart diagnostics
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz NULL
)
chat_run_events (
  id            bigint PK autoincrement,  -- doubles as SSE event id (Last-Event-ID)
  run_id        text NOT NULL FK chat_runs(id) ON DELETE CASCADE,
  position      int NOT NULL,             -- per-run sequence, unique with run_id
  event         text NOT NULL,
  payload       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, position)
)
```

`event`/`payload` store exactly what today's SSE emits. Delta events are
**batched** before persistence (flush at most every ~250 ms or 32 deltas, plus
on non-delta events) so token streaming does not become one INSERT per token;
each batched row's payload is the concatenated text, and the SSE view splits
nothing — it replays rows as-is (a reconnected client receives a few
coarser deltas for the replayed prefix, then fine-grained live deltas; the
message content is identical either way).

New module `src/covalent/runtime/run_manager.py`:

```python
class RunManager:
    """Owns background run execution, the event bus, and replay."""
    async def start_run(spec: RunSpec) -> str            # insert row, spawn asyncio task
    async def stream_run_events(run_id, after_position)  # replay from DB, then tail bus
    async def cancel_run(run_id) -> bool                 # status → cancelling, cancel task
    async def get_run(run_id) -> RunRecord
```

In-process bus: `{run_id: asyncio.Queue set}` per run; the worker pushes each
event (after DB persist) to every subscriber queue. Non-`run` events (e.g.
the terminal `session` snapshot event) close the run's subscriber set.

Worker task (`_execute_run`): runs today's `event_stream()` body —
transcript/activity accumulation, `service.stream(...)`, `finally`-block
session save — unchanged in substance, but writing each event to
`chat_run_events` + bus instead of yielding it over SSE. On client-triggered
cancellation: `cancel_run` sets the DB status, calls `task.cancel()`, and the
worker's `CancelledError` handler persists partial transcript/memory (the
ReAct loop's `_persist_session_messages` sites already persist at every pause
point; on cancellation the runtime additionally persists the messages
accumulated so far), writes a `cancelled` terminal event, and updates the run
row. On unexpected exception: `failed` + `error` event, same as today's
`except` blocks.

Concurrency guard: one active run per session. `start_run` rejects with 409
if an open run exists for the session (mirrors today's implicit behavior
where a second stream overwrites the first's state).

### 3. API surface

New endpoints (console-auth, session ownership-checked like today):

- `POST /agents/{agent_name}/runs` — body identical to `AgentRunRequest`.
  Creates/loads the session exactly as `stream_agent` does today (including
  pending-input resume logic), starts the run, returns
  `{"run_id": "...", "session_id": "..."}` immediately.
- `GET /agents/{agent_name}/runs/{run_id}/events` — SSE. Headers include
  `id:` lines (the event row's `position`). Honors `Last-Event-ID` (or
  `?after=` for clients whose proxies strip it): replays `chat_run_events`
  rows with `position > after`, then tails the bus until a terminal event.
  Terminal events (`final`, `error`, `cancelled`, `input_required`,
  `parent_input_required`, `session`) end the response after being sent.
- `POST /agents/{agent_name}/runs/{run_id}/cancel` — idempotent; 200 if the
  run was live (now cancelling), 200 if already terminal, 404 unknown run.
- `GET /agents/{agent_name}/runs` — list runs for a session (status + ids),
  so the UI can find and reattach to a still-running background run.

`POST /agents/{name}/stream` is reimplemented as: create run → immediately
stream its events (same connection, no separate GET). Behavior-compatible
for existing consumers, including its error-event and session-event shape.
When the client disconnects on this endpoint, the run **continues**
(consistent with the new model; disconnect ≠ cancel).

`input_required` (HITL pause): the run ends with status `completed` (the turn
paused waiting for input, as today's stream also just ends). The next
`POST /runs` supplies the resume tool result as today's `stream_agent` does.

### 4. Frontend changes

`frontend/lib/client-api.ts`:

- `startAgentRun(agentName, request): Promise<{runId, sessionId}>`
- `streamRunEvents(agentName, runId, {lastEventId, onChunk, signal})` — same
  SSE parser as `streamAgent`, extended to capture `id:` lines and expose the
  latest event id; on abort it just closes the view.
- `cancelAgentRun(agentName, runId)`
- `streamAgent` retained for compatibility (used by any non-console callers).

`chat-workspace.tsx`:

- Send button → `startAgentRun` then `streamRunEvents`. Keep the run id in
  state; on chunk, apply `assistant_delta` by appending to the streaming
  assistant message (same code path as today's `assistant` append, which
  already accumulates full-text updates — deltas append fragment-wise).
- Stop button → `cancelAgentRun` (keep the local AbortController only to
  close the SSE view cleanly). On `cancelled` event, mark the assistant
  message as stopped and keep partial text.
- Page return / remount: if the session has an open run (from
  `GET .../runs`), reattach with `Last-Event-ID = 0` (full replay) but
  **reconstruct transcript state idempotently** — replaying into an empty
  in-memory thread is the normal path, since the component rebuilds messages
  from the replayed events exactly like a live run. On remount with an
  existing thread already loaded, dedupe by replaying with
  `after = <last seen event id>` persisted per session in component state +
  the session record's transcript.
- `assistant_delta` handling in `updateThread`: append `payload.text` to the
  message identified by the run's assistant message id; `assistant` events
  still replace full text (idempotent reconciliation after reconnect).

### 5. Cancellation semantics

- `cancel` is only meaningful while `status = running`. Terminal runs return
  200 (idempotent no-op).
- The worker checks cancellation at event boundaries and via
  `task.cancel()` propagating `CancelledError` into the in-flight
  `adapter.stream()`/tool await. The model stream's underlying HTTP
  connection is closed (openai SDK cancellation), so no tokens are billed
  beyond the cancel point beyond provider latency.
- Partial output: memory messages accumulated to the cancel point are
  persisted (so the next turn has context), the transcript keeps the partial
  assistant text, and a `cancelled` terminal event is written. The session
  title/preview logic in the `finally` block runs normally.
- A cancelled run must not leave `chat_sessions` pending-input state behind
  unless the cancel arrived exactly during `ask_user` handling — in that
  window the run is already terminal (`input_required` ends the turn), so
  cancel returns the idempotent no-op.

### 6. Edge cases

- **Server restart with live runs:** on startup, `RunManager` marks every
  `running`/`cancelling` row older than boot as `failed` with
  `{"code": "server_restarted"}` and appends an `error` event so any
  reconnecting client sees the failure. The replay log makes the partial
  transcript recoverable for the session view.
- **Run orphans (session deleted):** FK cascade deletes run rows and events.
- **Two tabs on one session:** the second tab's `POST /runs` gets 409
  (one live run per session); it may still *view* the run's events.
- **Provider without streaming:** synthetic single delta (§1 fallback).
- **Delegate sub-streams:** `delegate_assistant` events today carry full
  text; delegates keep non-streaming generation (non-goal), so nothing
  changes there.
- **SSE proxy buffering:** existing `X-Accel-Buffering: no` headers carried
  over; delta batching keeps event volume ~4/s worst case on the wire.

## Implementation plan

1. **Runtime delta streaming** (react.py + model adapter aggregation helper):
   new `assistant_delta` event, streaming final answers, provider fallback.
   Unit tests: aggregation equals `generate()` shape; tool-call supersede.
2. **Runs infrastructure**: alembic migration, `RunManager`, event batching,
   bus, replay, startup sweep. Unit tests: replay continuity after
   Last-Event-ID, cancellation persistence, restart sweep.
3. **Routes**: `POST /runs`, `GET /runs/{id}/events`, `POST /runs/{id}/cancel`,
   `GET /runs`; rewire `stream_agent` onto RunManager. Route tests for auth,
   ownership, 409 double-run, resume-pending-input path.
4. **Frontend**: client-api additions, chat-workspace reattach + stop +
   delta rendering. Manual verification in browser.
5. **Docs**: update `docs/agent-stream-history-and-hitl.md` cross-links, note
   the run-centric lifecycle.

## Risks / trade-offs

- **Event volume in Postgres.** Bounded by batching (§2); a long run produces
  hundreds of rows, not millions. If this ever becomes hot, the delta rows
  can be collapsed post-completion (a `finished_at`-triggered compaction is a
  later optimization, deliberately out of scope).
- **In-process worker dies with the process.** Accepted: single-instance
  deployment, failure surfaced as `failed` runs. Migration to a queue is a
  later, isolated change behind `RunManager`.
- **Dual write (DB row + bus) ordering.** Persist first, then publish: a
  reconnecting client re-reads from the DB, so it can never miss an event it
  was told about via Last-Event-ID.
- **`assistant` full-text events coexisting with deltas.** Frontend treats
   `assistant` as authoritative replace; deltas are append-only between
   them. Reconnect replay may repeat a delta batch boundary — replaced, not
   duplicated, because replay always starts from a persisted event id.
