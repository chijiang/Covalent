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
