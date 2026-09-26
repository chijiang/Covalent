import { useEffect, useMemo, useRef, useState } from "react";
import {
  buildTraceEntries,
  buildTraceTurnGroups,
  formatTracePayload,
  getBaseEventTitle,
  getModelCallRawPayload,
  getTraceDisplayPayload,
  getTraceSummary,
  type EnrichedTraceEntry,
  type ModelRawPanel,
  type TraceActivity,
  type TraceNode,
} from "./trace";

const TRACE_PAYLOAD_PREVIEW_CHARS = 600;

type RawFetcher = (activityId: string) => Promise<TraceActivity>;

export function TracePanel({
  activity,
  userMessages,
  onFetchRaw,
}: {
  activity: TraceActivity[];
  userMessages: { content: unknown }[];
  onFetchRaw: RawFetcher;
}) {
  const entries = useMemo(() => buildTraceEntries(activity), [activity]);
  const turnGroups = useMemo(
    () => buildTraceTurnGroups(entries, userMessages),
    [entries, userMessages],
  );
  const [collapsedTurns, setCollapsedTurns] = useState<Set<number>>(new Set());

  function toggleTurn(turnIndex: number) {
    setCollapsedTurns((current) => {
      const next = new Set(current);
      if (next.has(turnIndex)) {
        next.delete(turnIndex);
      } else {
        next.add(turnIndex);
      }
      return next;
    });
  }

  return (
    <>
      <div className="surface-heading">
        <span>Execution trace</span>
        <span className="trace-heading-count">
          {entries.length} event{entries.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="trace-feed">
        {turnGroups.length === 0 ? (
          <div className="list-empty">
            <strong>No trace events yet</strong>
            <p>Trace events will appear here while the agent runs.</p>
          </div>
        ) : (
          turnGroups.map((turn) => {
            const isCollapsed = collapsedTurns.has(turn.turnIndex);
            return (
              <section
                className={`trace-turn-group${isCollapsed ? " is-collapsed" : ""}`}
                key={`turn-${turn.turnIndex}`}
              >
                <button
                  type="button"
                  className="trace-turn-header"
                  aria-expanded={!isCollapsed}
                  onClick={() => toggleTurn(turn.turnIndex)}
                >
                  <span className="trace-turn-label">Turn {turn.turnIndex}</span>
                  <span className="trace-turn-preview">{turn.userPreview}</span>
                  <span className="trace-turn-count">
                    {turn.entries.length} step
                    {turn.entries.length === 1 ? "" : "s"}
                  </span>
                  <span className="trace-turn-action">
                    {isCollapsed ? "Expand" : "Collapse"}
                  </span>
                </button>
                {!isCollapsed && (
                  <div className="trace-turn-steps">
                    <TraceNodeList nodes={turn.entries} onFetchRaw={onFetchRaw} />
                  </div>
                )}
              </section>
            );
          })
        )}
      </div>
    </>
  );
}

function TraceNodeList({
  nodes,
  onFetchRaw,
}: {
  nodes: TraceNode[];
  onFetchRaw: RawFetcher;
}) {
  return (
    <>
      {nodes.map((node) =>
        node.kind === "event" ? (
          <TraceStepEntry
            entry={node.entry}
            key={node.entry.id}
            onFetchRaw={onFetchRaw}
          />
        ) : (
          <TraceDelegateGroup
            key={`delegate-${node.firstItemId}`}
            node={node}
            onFetchRaw={onFetchRaw}
          />
        ),
      )}
    </>
  );
}

function TraceDelegateGroup({
  node,
  onFetchRaw,
}: {
  node: Extract<TraceNode, { kind: "delegate" }>;
  onFetchRaw: RawFetcher;
}) {
  const [expanded, setExpanded] = useState(false);
  const eventCount = node.children.length;
  const sourceLabel = node.agentName
    ? node.delegatedBy
      ? `${node.agentName} via ${node.delegatedBy}`
      : node.agentName
    : "subagent";
  return (
    <div className="trace-delegate-group" data-depth={node.depth}>
      <button
        type="button"
        className="trace-step trace-delegate-header"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="trace-step-line">
          <span className="trace-step-actor is-subagent">Subagent</span>
          <span className="trace-step-event">subagent</span>
          <span className="trace-step-summary">
            {sourceLabel} · {eventCount} event{eventCount === 1 ? "" : "s"}
            {node.runShortId ? ` · ${node.runShortId}` : ""}
          </span>
          <span className="trace-step-time">depth {node.depth}</span>
          <span className="trace-step-payload-toggle">
            {expanded ? "hide" : "show"}
          </span>
        </span>
      </button>
      {expanded && (
        <div className="trace-delegate-children">
          <TraceNodeList nodes={node.children} onFetchRaw={onFetchRaw} />
        </div>
      )}
    </div>
  );
}

function TraceStepEntry({
  entry: item,
  onFetchRaw,
}: {
  entry: EnrichedTraceEntry;
  onFetchRaw: RawFetcher;
}) {
  const [expanded, setExpanded] = useState(false);
  const [rawPanel, setRawPanel] = useState<ModelRawPanel | null>(null);
  const [fetchedRaw, setFetchedRaw] = useState<
    Partial<Record<ModelRawPanel, unknown>>
  >({});
  const [rawLoading, setRawLoading] = useState(false);
  const [rawFetchFailed, setRawFetchFailed] = useState(false);
  const rawFetchStartedRef = useRef(false);
  const isModelCall = getBaseEventTitle(item.title) === "model_call";
  const payloadRequest = getModelCallRawPayload(item.payload, "request");
  const payloadResponse = getModelCallRawPayload(item.payload, "response");
  const hasServerRaw = Boolean(item.has_raw_request || item.has_raw_response);
  // Raw blobs are stripped from session responses; fetch them once when the
  // step is expanded.
  useEffect(() => {
    if (
      !expanded ||
      !isModelCall ||
      !hasServerRaw ||
      rawFetchFailed ||
      rawFetchStartedRef.current
    ) {
      return;
    }
    if (payloadRequest !== null && payloadResponse !== null) {
      return;
    }
    rawFetchStartedRef.current = true;
    let active = true;
    setRawLoading(true);
    onFetchRaw(item.id)
      .then((detail) => {
        if (!active) return;
        const payload =
          detail.payload && typeof detail.payload === "object"
            ? (detail.payload as Record<string, unknown>)
            : {};
        setFetchedRaw((current) => ({
          ...current,
          request:
            payload.raw_request !== undefined
              ? payload.raw_request
              : current.request,
          response:
            payload.raw_response !== undefined
              ? payload.raw_response
              : current.response,
        }));
      })
      .catch(() => {
        if (active) setRawFetchFailed(true);
      })
      .finally(() => {
        if (active) setRawLoading(false);
      });
    return () => {
      active = false;
    };
  }, [
    expanded,
    hasServerRaw,
    isModelCall,
    item.id,
    onFetchRaw,
    payloadRequest,
    payloadResponse,
    rawFetchFailed,
  ]);

  const rawRequest =
    item.has_raw_request === false
      ? null
      : (payloadRequest ?? fetchedRaw.request ?? null);
  const rawResponse =
    item.has_raw_response === false
      ? null
      : (payloadResponse ?? fetchedRaw.response ?? null);
  const payloadText = formatTracePayload(
    getTraceDisplayPayload(item.title, item.payload),
  );
  const rawPayload =
    rawPanel === "request"
      ? rawRequest
      : rawPanel === "response"
        ? rawResponse
        : null;
  const isLongPayload = payloadText.length > TRACE_PAYLOAD_PREVIEW_CHARS;
  const displayPayload =
    isLongPayload && !expanded
      ? `${payloadText.slice(0, TRACE_PAYLOAD_PREVIEW_CHARS).trimEnd()}...`
      : payloadText;
  const summary = getTraceSummary(item);

  return (
    <div
      className={`trace-step is-actor-${item.actor}${expanded ? " is-expanded" : ""}`}
    >
      <div className="trace-step-line">
        <span className={`trace-step-actor is-${item.actor}`}>
          {item.actorLabel}
        </span>
        <span className="trace-step-event">{item.label}</span>
        <span className="trace-step-summary">{summary || item.eventTitle}</span>
        <span className="trace-step-time">{item.displayTime}</span>
        <button
          type="button"
          className="trace-step-payload-toggle"
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "hide" : "payload"}
        </button>
      </div>
      {expanded && (
        <div className="trace-step-details">
          <pre className="trace-step-payload">{displayPayload}</pre>
          {isModelCall && rawLoading && (
            <p className="trace-step-raw-loading">Loading raw payload…</p>
          )}
          {isModelCall && rawFetchFailed && hasServerRaw && (
            <p className="trace-step-raw-loading">Raw payload unavailable.</p>
          )}
          {isModelCall && (rawRequest !== null || rawResponse !== null) && (
            <div className="trace-step-raw">
              <div className="trace-step-raw-controls">
                {rawRequest !== null && (
                  <button
                    type="button"
                    className={`trace-step-raw-toggle${rawPanel === "request" ? " is-active" : ""}`}
                    onClick={() =>
                      setRawPanel((current) =>
                        current === "request" ? null : "request",
                      )
                    }
                  >
                    Raw request
                  </button>
                )}
                {rawResponse !== null && (
                  <button
                    type="button"
                    className={`trace-step-raw-toggle${rawPanel === "response" ? " is-active" : ""}`}
                    onClick={() =>
                      setRawPanel((current) =>
                        current === "response" ? null : "response",
                      )
                    }
                  >
                    Raw response
                  </button>
                )}
              </div>
              {rawPayload !== null && (
                <pre className="trace-step-payload trace-step-raw-payload">
                  {formatTracePayload(rawPayload)}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
