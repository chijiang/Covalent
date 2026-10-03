// Trace rendering helpers, kept in step with the Enterprise web renderer
// (frontend/components/chat-workspace.tsx). Desktop stores each entry's turn
// index because its conversation messages carry no ids or timestamps.
export type TraceActor = "agent" | "model" | "system" | "tool" | "user";
export type ModelRawPanel = "request" | "response";

export type TraceActivity = {
  id: string;
  title: string;
  payload: unknown;
  turn: number;
  has_raw_request?: boolean;
  has_raw_response?: boolean;
};

export type EnrichedTraceEntry = TraceActivity & {
  label: string;
  displayTime: string;
  actor: TraceActor;
  actorLabel: string;
  eventTitle: string;
};

export type TraceNode =
  | { kind: "event"; entry: EnrichedTraceEntry }
  | {
      kind: "delegate";
      toolCallId: string;
      runShortId: string | null;
      agentName: string;
      delegatedBy: string;
      depth: number;
      firstItemId: string;
      children: TraceNode[];
    };

export type TraceTurnGroup = {
  turnIndex: number;
  userPreview: string;
  entries: TraceNode[];
};

type DelegateTraceMetadata = {
  agent_name?: string | null;
  delegated_by?: string | null;
  delegate_tool_name?: string | null;
  delegate_tool_call_id?: string | null;
  delegate_run_id?: string | null;
  parent_delegate_run_id?: string | null;
  status?: string | null;
  delegation_depth?: number | null;
  parent_iteration?: number | null;
};

const DELEGATE_EVENT_PREFIX = "delegate_";
const MAX_DELEGATE_NESTING_DEPTH = 5;

// Base titles (prefix-stripped) of the stateful delegate lifecycle events.
// They are accepted as trace events — and given lifecycle labels and summaries —
// only when the full title carries the delegate_ prefix, so a root event
// literally named "created" or "idle" never matches.
const DELEGATE_LIFECYCLE_BASE_TITLES = new Set([
  "created",
  "running",
  "waiting_parent",
  "resumed",
  "idle",
  "released",
  "cancelled",
  "expired",
  "failed",
]);

export function isDelegateEventTitle(value: string): boolean {
  return value.startsWith(DELEGATE_EVENT_PREFIX);
}

export function getBaseEventTitle(value: string): string {
  return isDelegateEventTitle(value)
    ? value.slice(DELEGATE_EVENT_PREFIX.length)
    : value;
}

function asTracePayloadRecord(payload: unknown): Record<string, unknown> | null {
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : null;
}

export function getTimestampFromId(value: string, fallback: number): number {
  const timestamp = Number(value.split("-")[1]);
  return Number.isFinite(timestamp) ? timestamp : fallback;
}

function formatActivityTitle(value: string): string {
  return value
    .split("_")
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatTime(value: number): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(value);
}

function formatCompactNumber(value: number): string {
  if (Math.abs(value) >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}m`;
  }
  if (Math.abs(value) >= 1_000) {
    return `${(value / 1_000).toFixed(1)}k`;
  }
  return `${value}`;
}

function formatDurationLabel(value: unknown): string | null {
  const duration = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(duration) || duration <= 0) {
    return null;
  }
  if (duration >= 1000) {
    return `${(duration / 1000).toFixed(duration >= 10_000 ? 0 : 1)}s`;
  }
  return `${Math.round(duration)}ms`;
}

function truncateTraceSummaryText(value: string, maxChars = 240): string {
  const normalized = value.trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(maxChars - 3, 1)).trimEnd()}...`;
}

function readDelegateMeta(payload: unknown): DelegateTraceMetadata | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const record = payload as Record<string, unknown>;
  const toolCallId = record.delegate_tool_call_id;
  const runId = record.delegate_run_id;
  const hasToolCallId = typeof toolCallId === "string" && toolCallId.length > 0;
  const hasRunId = typeof runId === "string" && runId.length > 0;
  // Stateful delegate events carry a stable delegate_run_id alongside the
  // starting tool call id; legacy events only carry the tool call id.
  if (!hasToolCallId && !hasRunId) {
    return null;
  }
  return {
    agent_name: typeof record.agent_name === "string" ? record.agent_name : null,
    delegated_by:
      typeof record.delegated_by === "string" ? record.delegated_by : null,
    delegate_tool_name:
      typeof record.delegate_tool_name === "string"
        ? record.delegate_tool_name
        : null,
    delegate_tool_call_id: hasToolCallId ? toolCallId : null,
    delegate_run_id: hasRunId ? runId : null,
    parent_delegate_run_id:
      typeof record.parent_delegate_run_id === "string"
        ? record.parent_delegate_run_id
        : null,
    status: typeof record.status === "string" ? record.status : null,
    delegation_depth:
      typeof record.delegation_depth === "number"
        ? record.delegation_depth
        : null,
    parent_iteration:
      typeof record.parent_iteration === "number"
        ? record.parent_iteration
        : null,
  };
}

function readChildToolCallIdsFromPayload(payload: unknown): string[] {
  // A delegate_tool_calls event carries the child tool calls its subagent
  // issued; those child ids are the only way to reconstruct parent→child
  // nesting because each delegate event only carries its immediate parent's
  // tool_call_id.
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const record = payload as Record<string, unknown>;
  const calls = record.tool_calls;
  if (!Array.isArray(calls)) {
    return [];
  }
  const ids: string[] = [];
  for (const call of calls) {
    if (call && typeof call === "object") {
      const id = (call as Record<string, unknown>).id;
      if (typeof id === "string" && id.length > 0) {
        ids.push(id);
      }
    }
  }
  return ids;
}

// Build a trace tree from a flat, possibly-interleaved list of entries.
// Entries whose payload carries a non-empty delegate_tool_call_id are grouped
// into per-id delegate nodes; all other entries stay as top-level event nodes.
// Parent→child nesting is reconstructed from delegate_tool_calls payloads.
// Beyond MAX_DELEGATE_NESTING_DEPTH, deeper delegate nodes flatten into their
// parent's children list rather than nesting further.
// ancestorIds is the set of delegate ids currently being built up the call
// stack — those ids' own events are rendered as plain children inside the
// current node, never re-grouped into another delegate node (prevents infinite
// recursion: a delegate's own events all carry its own id).
function buildTraceTree(
  items: EnrichedTraceEntry[],
  depth = 1,
  ancestorIds: ReadonlySet<string> = new Set(),
): TraceNode[] {
  if (items.length === 0) {
    return [];
  }

  // Pre-pass: map parent tool_call_id → list of child tool_call_ids declared in
  // any delegate_tool_calls event inside this slice.
  const declaredChildren = new Map<string, Set<string>>();
  for (const item of items) {
    if (getBaseEventTitle(item.title) !== "tool_calls") {
      continue;
    }
    const meta = readDelegateMeta(item.payload);
    if (!meta?.delegate_tool_call_id) {
      continue;
    }
    const childIds = readChildToolCallIdsFromPayload(item.payload);
    if (childIds.length === 0) {
      continue;
    }
    const parentId = meta.delegate_tool_call_id;
    const existing = declaredChildren.get(parentId) ?? new Set<string>();
    for (const cid of childIds) {
      existing.add(cid);
    }
    declaredChildren.set(parentId, existing);
  }

  // Determine which ids belong "inside" some other id in this slice.
  const nestedIds = new Set<string>();
  for (const childSet of declaredChildren.values()) {
    for (const cid of childSet) {
      nestedIds.add(cid);
    }
  }

  const buckets = new Map<string, EnrichedTraceEntry[]>();
  const bucketMeta = new Map<string, DelegateTraceMetadata>();
  const topLevelPlan: Array<
    { kind: "event"; entry: EnrichedTraceEntry } | { kind: "delegate"; id: string }
  > = [];
  const seenDelegateIds = new Set<string>();

  for (const item of items) {
    const isDelegateTitle = isDelegateEventTitle(item.title);
    const meta = isDelegateTitle ? readDelegateMeta(item.payload) : null;

    if (!meta) {
      topLevelPlan.push({ kind: "event", entry: item });
      continue;
    }

    // Group by the stable delegate_run_id when present so follow-up sends into
    // the same run share one trace node even though each leg carries a
    // different parent tool call id; legacy events fall back to the tool call id.
    const id = meta.delegate_run_id || meta.delegate_tool_call_id;
    if (!id) {
      topLevelPlan.push({ kind: "event", entry: item });
      continue;
    }
    // An id currently being built up the stack is this node's own id (or an
    // ancestor's): its events are rendered inline, not re-grouped.
    if (ancestorIds.has(id)) {
      topLevelPlan.push({ kind: "event", entry: item });
      continue;
    }
    if (!buckets.has(id)) {
      buckets.set(id, []);
      bucketMeta.set(id, meta);
    }
    buckets.get(id)!.push(item);

    // Emit one top-level slot per id at its first-seen position. Ids that are
    // declared as children of another id in this slice are NOT emitted at top
    // level — buildTraceTree recursing into the parent bucket will attach them.
    if (!seenDelegateIds.has(id) && !nestedIds.has(id)) {
      seenDelegateIds.add(id);
      topLevelPlan.push({ kind: "delegate", id });
    }
  }

  const result: TraceNode[] = [];
  for (const slot of topLevelPlan) {
    if (slot.kind === "event") {
      result.push({ kind: "event", entry: slot.entry });
    } else {
      result.push(
        buildDelegateNode(
          slot.id,
          bucketMeta.get(slot.id)!,
          buckets.get(slot.id)!,
          depth,
        ),
      );
    }
  }
  return result;
}

function buildDelegateNode(
  id: string,
  meta: DelegateTraceMetadata,
  items: EnrichedTraceEntry[],
  depth: number,
  ancestorIds: ReadonlySet<string> = new Set(),
): Extract<TraceNode, { kind: "delegate" }> {
  const childAncestors = new Set(ancestorIds);
  childAncestors.add(id);
  const runShortId = meta.delegate_run_id
    ? meta.delegate_run_id.slice(0, 12)
    : null;
  // Beyond the max depth, flatten: stop recursing, render children as a flat
  // event list inside this node to avoid runaway nesting.
  if (depth >= MAX_DELEGATE_NESTING_DEPTH) {
    return {
      kind: "delegate",
      toolCallId: id,
      runShortId,
      agentName: meta.agent_name ?? "",
      delegatedBy: meta.delegated_by ?? "",
      depth,
      firstItemId: items[0]?.id ?? "",
      children: items.map((entry) => ({ kind: "event" as const, entry })),
    };
  }
  return {
    kind: "delegate",
    toolCallId: id,
    runShortId,
    agentName: meta.agent_name ?? "",
    delegatedBy: meta.delegated_by ?? "",
    depth,
    firstItemId: items[0]?.id ?? "",
    children: buildTraceTree(items, depth + 1, childAncestors),
  };
}

function withTraceSourcePrefix(
  isDelegate: boolean,
  payload: Record<string, unknown>,
  summary: string,
): string {
  if (!isDelegate) {
    return summary;
  }

  const agentName =
    typeof payload.agent_name === "string" ? payload.agent_name.trim() : "";
  if (!agentName) {
    return `Subagent: ${summary}`;
  }

  const delegatedBy =
    typeof payload.delegated_by === "string" ? payload.delegated_by.trim() : "";
  const sourceLabel = delegatedBy ? `${agentName} via ${delegatedBy}` : agentName;
  return `Subagent ${sourceLabel}: ${summary}`;
}

export function getTraceSummary(item: {
  title: string;
  payload: unknown;
}): string | null {
  const payload = asTracePayloadRecord(item.payload);
  if (!payload) {
    return null;
  }
  const baseTitle = getBaseEventTitle(item.title);
  const isDelegate = isDelegateEventTitle(item.title);

  if (baseTitle === "context_window") {
    const originalMessages = Number(payload.original_message_count) || 0;
    const requestMessages = Number(payload.request_message_count) || 0;
    const summarized = Number(payload.summarized_message_count) || 0;
    const dropped = Number(payload.dropped_message_count) || 0;
    const truncated = Number(payload.truncated_message_count) || 0;
    const phase = typeof payload.phase === "string" ? payload.phase : "react";
    const method =
      typeof payload.compaction_method === "string"
        ? payload.compaction_method
        : "";
    const estimatedTokens = Number(payload.estimated_prompt_tokens) || 0;
    const tokenBudget = Number(payload.token_budget) || 0;
    const tokenInfo =
      estimatedTokens && tokenBudget
        ? ` ~${formatCompactNumber(estimatedTokens)}/${formatCompactNumber(tokenBudget)} tokens`
        : "";
    const methodLabel =
      method === "summarize"
        ? "LLM summarized"
        : method === "prune+summarize"
          ? "pruned + LLM summarized"
          : method === "prune"
            ? "pruned"
            : "compacted";
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      `${phase} ${methodLabel} ${originalMessages} to ${requestMessages} messages${tokenInfo}. Summarized ${summarized}, dropped ${dropped}, truncated ${truncated}.`,
    );
  }

  if (baseTitle === "model_call") {
    const provider =
      typeof payload.provider === "string" ? payload.provider : "provider";
    const model = typeof payload.model === "string" ? payload.model : "model";
    const phase = typeof payload.phase === "string" ? payload.phase : "react";
    const status = payload.status === "error" ? "failed" : "completed";
    const duration = formatDurationLabel(payload.elapsed_ms);
    const requestMessages = Number(payload.request_message_count) || 0;
    const promptTokens = Number(payload.prompt_tokens) || 0;
    const completionTokens = Number(payload.completion_tokens) || 0;
    const tokenInfo = promptTokens
      ? ` (${formatCompactNumber(promptTokens)}+${formatCompactNumber(completionTokens)} tokens)`
      : "";
    const detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
    const base = `${provider} / ${model} ${phase} call ${status}${duration ? ` in ${duration}` : ""} with ${requestMessages} messages${tokenInfo}.`;
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      detail ? `${base} ${detail}` : base,
    );
  }

  if (baseTitle === "tool_calls") {
    const toolCalls = Array.isArray(payload.tool_calls)
      ? payload.tool_calls.length
      : 0;
    if (!toolCalls) {
      return isDelegate
        ? withTraceSourcePrefix(isDelegate, payload, "Requested tool execution.")
        : null;
    }
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      `Requested ${toolCalls} tool call${toolCalls === 1 ? "" : "s"}.`,
    );
  }

  if (baseTitle === "tool_results") {
    const results = Array.isArray(payload.results) ? payload.results.length : 0;
    if (!results) {
      return isDelegate
        ? withTraceSourcePrefix(isDelegate, payload, "Collected tool results.")
        : null;
    }
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      `Collected ${results} tool result${results === 1 ? "" : "s"}.`,
    );
  }

  if (baseTitle === "thought") {
    const summary = typeof payload.summary === "string" ? payload.summary.trim() : "";
    return summary ? withTraceSourcePrefix(isDelegate, payload, summary) : null;
  }

  if (baseTitle === "iteration") {
    const iteration = Number(payload.iteration) || 0;
    const base = iteration
      ? `Started ReAct iteration ${iteration}.`
      : "Started a ReAct iteration.";
    return withTraceSourcePrefix(isDelegate, payload, base);
  }

  if (baseTitle === "assistant") {
    const text = typeof payload.text === "string" ? payload.text.trim() : "";
    return text
      ? withTraceSourcePrefix(
          isDelegate,
          payload,
          `Streaming response: ${truncateTraceSummaryText(text)}`,
        )
      : null;
  }

  if (baseTitle === "final") {
    const text =
      typeof payload.output_text === "string" ? payload.output_text.trim() : "";
    return text
      ? withTraceSourcePrefix(
          isDelegate,
          payload,
          `Completed with final response: ${truncateTraceSummaryText(text)}`,
        )
      : withTraceSourcePrefix(isDelegate, payload, "Completed with final response.");
  }

  if (baseTitle === "input_required") {
    const title = typeof payload.title === "string" ? payload.title.trim() : "";
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      title ? `Paused for input: ${title}` : "Paused for input.",
    );
  }

  if (baseTitle === "error") {
    const detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
    return detail ? withTraceSourcePrefix(isDelegate, payload, detail) : null;
  }

  if (isDelegate && DELEGATE_LIFECYCLE_BASE_TITLES.has(baseTitle)) {
    const summary =
      typeof payload.summary === "string" ? payload.summary.trim() : "";
    if (baseTitle === "waiting_parent") {
      return withTraceSourcePrefix(
        isDelegate,
        payload,
        summary
          ? `Waiting for parent: ${truncateTraceSummaryText(summary)}`
          : "Waiting for parent.",
      );
    }
    if (baseTitle === "idle") {
      return withTraceSourcePrefix(
        isDelegate,
        payload,
        summary ? `Returned: ${truncateTraceSummaryText(summary)}` : "Returned.",
      );
    }
    if (
      baseTitle === "released" ||
      baseTitle === "cancelled" ||
      baseTitle === "expired" ||
      baseTitle === "failed"
    ) {
      const terminalLabel =
        baseTitle === "released"
          ? "Released"
          : baseTitle === "cancelled"
            ? "Cancelled"
            : baseTitle === "expired"
              ? "Expired"
              : "Failed";
      return withTraceSourcePrefix(
        isDelegate,
        payload,
        summary
          ? `${terminalLabel}: ${truncateTraceSummaryText(summary)}`
          : `${terminalLabel}.`,
      );
    }
    // created / running / resumed carry no dedicated summary; the entry falls
    // back to its event title.
    return null;
  }

  return null;
}

function isAskUserToolResult(payload: Record<string, unknown> | null): boolean {
  if (!payload || !Array.isArray(payload.results)) {
    return false;
  }
  return payload.results.some((result) => {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      return false;
    }
    const record = result as Record<string, unknown>;
    return record.input_request !== undefined && record.input_request !== null;
  });
}

function getTraceActor(title: string, rawPayload?: unknown): TraceActor {
  const baseTitle = getBaseEventTitle(title);
  if (baseTitle === "model_call") {
    return "model";
  }
  if (baseTitle === "user_message" || baseTitle === "input_resolved") {
    return "user";
  }
  if (baseTitle === "tool_results") {
    return isAskUserToolResult(asTracePayloadRecord(rawPayload)) ? "user" : "tool";
  }
  if (
    baseTitle === "assistant" ||
    baseTitle === "final" ||
    baseTitle === "tool_calls"
  ) {
    return "agent";
  }
  if (baseTitle === "thought") {
    const payload = asTracePayloadRecord(rawPayload);
    return payload?.kind === "model_reasoning" ? "agent" : "system";
  }
  if (
    baseTitle === "input_required" ||
    baseTitle === "context_window" ||
    baseTitle === "error" ||
    baseTitle === "iteration"
  ) {
    return "system";
  }
  return "agent";
}

function getTraceActorLabel(actor: TraceActor): string {
  if (actor === "model") {
    return "Model";
  }
  if (actor === "system") {
    return "System";
  }
  if (actor === "tool") {
    return "Tool";
  }
  if (actor === "user") {
    return "User";
  }
  return "Agent";
}

export function getTraceEventLabel(title: string, rawPayload?: unknown): string {
  const baseTitle = getBaseEventTitle(title);
  const isDelegate = isDelegateEventTitle(title);
  if (isDelegate) {
    if (baseTitle === "waiting_parent") {
      return "Subagent · waiting for parent";
    }
    if (baseTitle === "idle") {
      return "Subagent · idle";
    }
    if (
      baseTitle === "released" ||
      baseTitle === "cancelled" ||
      baseTitle === "expired" ||
      baseTitle === "failed"
    ) {
      return "Subagent · ended";
    }
    return "Subagent";
  }
  if (baseTitle === "error") {
    return "Error";
  }
  if (baseTitle === "tool_results") {
    return "Result";
  }
  if (baseTitle === "tool_calls") {
    return "Tool";
  }
  if (baseTitle === "iteration") {
    return "Iteration";
  }
  if (baseTitle === "thought") {
    const payload = asTracePayloadRecord(rawPayload);
    return payload?.kind === "model_reasoning" ? "Reasoning" : "Event";
  }
  if (baseTitle === "model_call") {
    return "Call";
  }
  if (baseTitle === "context_window") {
    return "Context";
  }
  if (baseTitle === "assistant") {
    return "Stream";
  }
  if (baseTitle === "final") {
    return "Final";
  }
  if (baseTitle === "user_message") {
    return "Query";
  }
  if (baseTitle === "input_required" || baseTitle === "input_resolved") {
    return "Input";
  }
  return "Event";
}

export function getTraceDisplayPayload(title: string, payload: unknown): unknown {
  if (getBaseEventTitle(title) !== "model_call") {
    return payload;
  }
  const record = asTracePayloadRecord(payload);
  if (!record) {
    return payload;
  }
  const displayPayload = { ...record };
  delete displayPayload.raw_request;
  delete displayPayload.raw_response;
  return displayPayload;
}

export function getModelCallRawPayload(
  payload: unknown,
  panel: ModelRawPanel,
): unknown | null {
  const record = asTracePayloadRecord(payload);
  if (!record) {
    return null;
  }
  const rawPayload = panel === "request" ? record.raw_request : record.raw_response;
  return rawPayload === undefined || rawPayload === null ? null : rawPayload;
}

export function formatTracePayload(payload: unknown): string {
  return typeof payload === "string"
    ? payload
    : `${JSON.stringify(payload, null, 2)}\n`;
}

function getTurnUserPreview(message: { content: unknown }): string {
  const content =
    typeof message.content === "string"
      ? message.content.trim()
      : JSON.stringify(message.content ?? "").trim();
  return content ? truncateTraceSummaryText(content, 96) : "User message";
}

export function buildTraceEntries(
  activity: TraceActivity[],
  fallbackTimestamp = Date.now(),
): EnrichedTraceEntry[] {
  return [...activity]
    .sort(
      (left, right) =>
        getTimestampFromId(left.id, fallbackTimestamp) -
        getTimestampFromId(right.id, fallbackTimestamp),
    )
    .map((item) => {
      const actor = getTraceActor(item.title, item.payload);
      return {
        ...item,
        label: getTraceEventLabel(item.title, item.payload),
        displayTime: formatTime(getTimestampFromId(item.id, fallbackTimestamp)),
        actor,
        actorLabel: getTraceActorLabel(actor),
        eventTitle: formatActivityTitle(getBaseEventTitle(item.title)),
      };
    });
}

export function buildTraceTurnGroups(
  entries: EnrichedTraceEntry[],
  userMessages: { content: unknown }[],
): TraceTurnGroup[] {
  if (entries.length === 0) {
    return [];
  }
  const buckets = new Map<number, EnrichedTraceEntry[]>();
  for (const entry of entries) {
    const turn = entry.turn > 0 ? entry.turn : 1;
    const bucket = buckets.get(turn);
    if (bucket) {
      bucket.push(entry);
    } else {
      buckets.set(turn, [entry]);
    }
  }
  return [...buckets.entries()]
    .sort(([left], [right]) => left - right)
    .map(([turnIndex, turnEntries]) => ({
      turnIndex,
      userPreview: previewForTurn(turnIndex, userMessages),
      entries: buildTraceTree(turnEntries),
    }));
}

function previewForTurn(
  turnIndex: number,
  userMessages: { content: unknown }[],
): string {
  if (turnIndex <= userMessages.length) {
    return getTurnUserPreview(userMessages[turnIndex - 1]);
  }
  // Turns started by answering an Agent question inject no new user message.
  return turnIndex === 1 ? "Initial run" : "Answered Agent question";
}
