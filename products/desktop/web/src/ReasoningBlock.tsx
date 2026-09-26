import { useState } from "react";

// Reasoning attribution markers shared with the sidecar writer
// (covalent_desktop.application.trace.REASONING_SOURCE_MARK): a U+001E-wrapped
// "#agent:<name>" token embedded in reasoning text switches the active segment
// source. An empty name switches back to the main agent; content without any
// marker parses as a single unattributed segment.
const REASONING_SOURCE_MARK = "\x1e";
const REASONING_SOURCE_PREFIX = "#agent:";

export type ReasoningSegment = {
  agentName: string | null;
  text: string;
};

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

export function ReasoningBlock({
  reasoning,
  active,
}: {
  reasoning: string;
  active: boolean;
}) {
  // null = follow the automatic policy: open while thinking streams and the
  // answer is still empty, collapsed once content arrives.
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? active;
  const segments = parseReasoningSegments(reasoning);
  return (
    <div className={`chat-reasoning-block${open ? " is-open" : ""}`}>
      <button
        aria-expanded={open}
        className="chat-reasoning-toggle"
        type="button"
        onClick={() => setManualOpen((current) => !(current ?? active))}
      >
        <span className="chat-reasoning-label">
          {active ? "Thinking…" : "Thought process"}
        </span>
        <span aria-hidden="true" className="chat-reasoning-action">
          {open ? "Collapse" : "Expand"}
        </span>
      </button>
      {open ? (
        <div className="chat-reasoning-content">
          {segments.map((segment, index) =>
            segment.agentName ? (
              <div className="chat-reasoning-segment" key={index}>
                <div className="chat-reasoning-agent">{segment.agentName}</div>
                {segment.text}
              </div>
            ) : (
              <div className="chat-reasoning-segment" key={index}>
                {segment.text}
              </div>
            ),
          )}
        </div>
      ) : null}
    </div>
  );
}
