// The sidecar stores the runtime's memory transcript, where every ReAct
// iteration is its own assistant message (text, then tool calls, then the
// final answer). Enterprise keeps a separate display transcript that merges a
// turn's legs into one assistant message; this module derives the same view for
// rendering, so a tool-using turn shows a single bubble.

export type PublishedKind = "image" | "pdf" | "text" | "binary";

export type PublishedFile = {
  id: string;
  name: string;
  size: number;
  contentType: string;
  summary: string;
  kind: PublishedKind;
};

export type DisplayMessage = {
  key: string;
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  files: PublishedFile[];
  turn: number;
  time: number | null;
  // 1-based index among user messages; the edit target for user bubbles.
  userOrdinal: number | null;
};

export function formatMessageTimestamp(value: number): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(value);
}

export function messageText(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content ?? "");
}

export function attachmentKind(contentType: string): PublishedKind {
  const normalized = contentType.trim().toLowerCase();
  if (normalized.startsWith("image/")) return "image";
  if (normalized === "application/pdf") return "pdf";
  if (normalized.startsWith("text/")) return "text";
  return "binary";
}

function fileFromPayload(value: unknown): PublishedFile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const payload = value as Record<string, unknown>;
  if (
    typeof payload.name !== "string" ||
    typeof payload.download_url !== "string"
  ) {
    return null;
  }
  const contentType =
    typeof payload.content_type === "string"
      ? payload.content_type
      : "application/octet-stream";
  return {
    id: typeof payload.id === "string" ? payload.id : payload.name,
    name: payload.name,
    size: Number(payload.size) || 0,
    contentType,
    summary:
      typeof payload.summary === "string" ? payload.summary : "Generated file",
    kind: attachmentKind(contentType),
  };
}

function fileFromText(text: string): PublishedFile | null {
  try {
    return fileFromPayload(JSON.parse(text));
  } catch {
    return null;
  }
}

// Tool results reach the transcript in a few shapes: publish_downloadable_file
// returns its payload as a JSON string, while browser tools return multimodal
// parts ([{type: "text", text: "<payload>"}]) or structured download_artifacts.
export function publishedFilesFrom(content: unknown): PublishedFile[] {
  if (typeof content === "string") {
    const file = fileFromText(content);
    return file ? [file] : [];
  }
  if (!Array.isArray(content)) {
    const file = fileFromPayload(content);
    return file ? [file] : [];
  }
  const files: PublishedFile[] = [];
  for (const part of content) {
    if (!part || typeof part !== "object") continue;
    const record = part as Record<string, unknown>;
    if (Array.isArray(record.download_artifacts)) {
      for (const artifact of record.download_artifacts) {
        const file = fileFromPayload(artifact);
        if (file) files.push(file);
      }
      continue;
    }
    if (typeof record.text === "string") {
      const file = fileFromText(record.text);
      if (file) files.push(file);
      continue;
    }
    const direct = fileFromPayload(record);
    if (direct) files.push(direct);
  }
  return files;
}

export function reasoningOf(message: DesktopMessage): string {
  return typeof message.reasoning_content === "string"
    ? message.reasoning_content
    : "";
}

// Everything before the ``userOrdinal``-th user message, i.e. the prefix the
// sidecar keeps when a message is edited and resent.
export function messagesBeforeUserOrdinal(
  messages: DesktopMessage[],
  userOrdinal: number,
): DesktopMessage[] {
  let seen = 0;
  for (let index = 0; index < messages.length; index += 1) {
    if (messages[index].role === "user") {
      seen += 1;
      if (seen === userOrdinal) {
        return messages.slice(0, index);
      }
    }
  }
  return messages;
}

export function buildDisplayMessages(
  messages: DesktopMessage[],
  turnMeta: DesktopTurnMeta[] = [],
): DisplayMessage[] {
  const display: DisplayMessage[] = [];
  const byUserOrdinal = new Map<number, DesktopTurnMeta>();
  for (const entry of turnMeta) {
    if (entry.user_ordinal !== null) {
      byUserOrdinal.set(entry.user_ordinal, entry);
    }
  }
  let turn: DisplayMessage | null = null;
  let turnNumber = 0;
  let turnTime: number | null = null;
  let userOrdinal = 0;

  messages.forEach((message, index) => {
    if (message.role === "user") {
      userOrdinal += 1;
      const opened = byUserOrdinal.get(userOrdinal);
      // Sessions recorded before turn metadata existed have no timestamp.
      turnNumber = opened?.turn ?? userOrdinal;
      turnTime = opened?.started_at ?? null;
      turn = null;
      display.push({
        key: `user-${index}`,
        role: "user",
        content: messageText(message.content),
        reasoning: "",
        files: [],
        turn: turnNumber,
        time: turnTime,
        userOrdinal,
      });
      return;
    }
    if (message.role === "assistant") {
      if (!turn) {
        turn = {
          key: `assistant-${index}`,
          role: "assistant",
          content: "",
          reasoning: "",
          files: [],
          turn: turnNumber,
          time: turnTime,
          userOrdinal: null,
        };
        display.push(turn);
      }
      const reasoning = reasoningOf(message);
      if (reasoning) {
        // Each leg's thinking arrives as its own block; keep them readable
        // rather than gluing sentence ends together.
        turn.reasoning += turn.reasoning ? `\n\n${reasoning}` : reasoning;
      }
      const text = messageText(message.content).trim();
      if (text) {
        // The last leg with text is the turn's answer; earlier legs only
        // narrated the tool calls Enterprise hides anyway.
        turn.content = text;
      }
      return;
    }
    if (message.role === "tool" && turn) {
      for (const file of publishedFilesFrom(message.content)) {
        if (!turn.files.some((item) => item.name === file.name)) {
          turn.files.push(file);
        }
      }
    }
  });

  // A turn that produced neither text, thinking nor a file (a bare tool call)
  // has nothing to show; its activity lives in the trace panel.
  return display.filter(
    (message) =>
      message.role === "user" ||
      Boolean(message.content || message.reasoning || message.files.length),
  );
}
