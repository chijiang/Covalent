/** Ephemeral output only; the persisted transcript replaces it on completion. */
export interface StreamState {
  text: string;
  reasoning: string;
  iteration: number | null;
  source: string;
  hasDeltas: boolean;
}
export const emptyStream = (): StreamState => ({
  text: "", reasoning: "", iteration: null, source: "", hasDeltas: false,
});

const REASONING_SOURCE_MARK = "\x1e";
const REASONING_SOURCE_PREFIX = "#agent:";
const LIVE_REASONING_VIEW_CHARS = 20_000;
const MAX_MARKER_CHARS = 128;

/** Display window for live reasoning: parsing and layout cost must stay
 * bounded while a long thinking stream appends per token, so render only the
 * tail. The persisted transcript keeps the full text. */
export function liveReasoningView(reasoning: string): string {
  if (reasoning.length <= LIVE_REASONING_VIEW_CHARS) return reasoning;
  let view = reasoning.slice(-LIVE_REASONING_VIEW_CHARS);
  if (view.startsWith(REASONING_SOURCE_MARK + REASONING_SOURCE_PREFIX)) {
    return view;
  }
  const markerEnd = view.indexOf(REASONING_SOURCE_MARK);
  if (markerEnd !== -1 && markerEnd <= MAX_MARKER_CHARS) {
    // The window edge cut a marker in half; drop the fragment up to its
    // closing delimiter so the segment parser never sees it.
    return view.slice(markerEnd + 1);
  }
  const stray = view.match(/^#agent:[^\n]*/);
  return stray ? view.slice(stray[0].length) : view;
}
export function applyStreamEvent(state: StreamState, event: {
  event: string; payload: Record<string, unknown>;
}): StreamState {
  const { payload } = event;
  const text = typeof payload.text === "string" ? payload.text : "";
  if (!text) return state;
  let next = state;
  if (event.event === "reasoning_delta" || event.event === "delegate_reasoning_delta") {
    const source = event.event === "delegate_reasoning_delta" && typeof payload.agent_name === "string"
      ? payload.agent_name : "";
    next = { ...state, source, reasoning: state.reasoning +
      (source !== state.source ? `\x1e#agent:${source}\x1e` : "") + text };
  } else if (event.event === "assistant_delta" || event.event === "assistant") {
    const iteration = typeof payload.iteration === "number" ? payload.iteration : 0;
    if (event.event === "assistant" && state.iteration === iteration && state.hasDeltas) return state;
    next = { ...state, iteration, hasDeltas: event.event === "assistant_delta",
      text: event.event === "assistant_delta" && state.iteration === iteration ? state.text + text : text };
  }
  if (next.text.length + next.reasoning.length > 2 * 1024 * 1024)
    throw new Error("Live output limit exceeded");
  return next;
}
