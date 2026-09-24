// Reasoning attribution markers shared with the backend
// (session_service.REASONING_SOURCE_MARK): a U+001E-wrapped "#agent:<name>"
// token embedded in reasoning_content switches the active segment source.
// An empty name switches back to the main agent; content without any marker
// predates attribution and parses as a single unattributed segment.

const REASONING_SOURCE_MARK = "\x1e";
const REASONING_SOURCE_PREFIX = "#agent:";

export type ReasoningSegment = {
  agentName: string | null;
  text: string;
};

export function serializeReasoningMarker(agentName: string | null): string {
  return `${REASONING_SOURCE_MARK}${REASONING_SOURCE_PREFIX}${agentName ?? ""}${REASONING_SOURCE_MARK}`;
}

export function parseReasoningSegments(reasoning: string): ReasoningSegment[] {
  const segments: ReasoningSegment[] = [];
  const push = (agentName: string | null, text: string) => {
    const last = segments[segments.length - 1];
    if (last && last.agentName === agentName) {
      last.text += text;
    } else {
      segments.push({ agentName, text });
    }
  };
  const pattern = new RegExp(
    `${REASONING_SOURCE_MARK}${REASONING_SOURCE_PREFIX}([^${REASONING_SOURCE_MARK}]*)${REASONING_SOURCE_MARK}`,
    "g",
  );
  let active: string | null = null;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(reasoning)) !== null) {
    const text = reasoning.slice(cursor, match.index);
    if (text) {
      push(active, text);
    }
    active = match[1] || null;
    cursor = match.index + match[0].length;
  }
  const tail = reasoning.slice(cursor);
  if (tail) {
    push(active, tail);
  }
  return segments;
}
