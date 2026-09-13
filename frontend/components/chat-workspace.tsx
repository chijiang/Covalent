"use client";

import {
  isValidElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type CSSProperties,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  ArrowUp,
  Loader2,
  PanelRight,
  PanelRightClose,
  Pencil,
  Plus,
  Square,
  Trash2,
  Upload,
} from "lucide-react";

import { useChatSessions } from "@/components/chat-sessions-provider";
import { useAuth } from "@/components/auth-provider";

import {
  cancelAgentRun,
  getAgents,
  getChatSession,
  getChatSessionActivity,
  getHealth,
  listAgentRuns,
  renameChatSession,
  replaceChatTranscript,
  sortAgentsForPicker,
  startAgentRun,
  streamAgentRunEvents,
  StreamAbortedError,
  uploadChatAttachments,
} from "@/lib/client-api";
import { uid, type ChatThread, type ChatThreadMessage } from "@/lib/chat-thread-model";
import { serializeReasoningMarker, parseReasoningSegments } from "@/lib/reasoning-segments";
import { stripActivityItem } from "@/lib/trace-payload";
import type {
  AgentDetail,
  AgentInputPart,
  AttachmentDeliveryMode,
  AttachmentUploadItem,
  ChatSession,
  ChatSessionMessage,
  ChatSessionSummary,
  DelegateRunStatus,
  HealthResponse,
  PendingQuestionRequest,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { toast } from "sonner";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  attachments?: ComposerAttachment[];
  askUserPrompt?: PendingQuestionRequest | null;
};

type ComposerAttachment = {
  id: string;
  name: string;
  size: number;
  type: string;
  lastModified: number;
  workspacePath?: string;
  downloadUrl?: string;
  uploadedAt?: string;
  deliveryMode?: AttachmentDeliveryMode;
  kind?: "text" | "image" | "pdf" | "binary";
  summary?: string;
  modelPromptText?: string;
  modelContent?: AgentInputPart[];
  pageCount?: number | null;
};

type ActivityItem = {
  id: string;
  title: string;
  payload: unknown;
  hasRawRequest?: boolean;
  hasRawResponse?: boolean;
};

type PendingQuestionDraft = {
  selectedOptions: string[];
  freeform: string;
};

const TRACE_PANEL_STORAGE_KEY = "agent-framework.chat-trace-width";
const TRACE_PANEL_VISIBLE_STORAGE_KEY = "agent-framework.chat-trace-visible";
const DEFAULT_TRACE_PANEL_WIDTH = 360;
// Transcript pagination: entering a session hydrates only the newest messages;
// scrolling to the top pages in older ones.
const MESSAGES_INITIAL_LIMIT = 3;
const MESSAGES_OLDER_PAGE_SIZE = 20;
const MESSAGES_FULL_LOAD_LIMIT = 1_000_000;
const MESSAGE_STAGE_TOP_TRIGGER_PX = 80;
const MIN_TRACE_PANEL_WIDTH = 280;
const MAX_TRACE_PANEL_WIDTH = 640;
const MIN_CONVERSATION_PANEL_WIDTH = 560;
const CODE_BLOCK_COPY_RESET_MS = 1600;
const TRACE_PAYLOAD_PREVIEW_CHARS = 600;

type ChatCodeBlockTone = "inbound" | "outbound";
type TraceActor = "agent" | "model" | "system" | "tool" | "user";
type ModelRawPanel = "request" | "response";

type EnrichedTraceEntry = ActivityItem & {
  label: string;
  displayTime: string;
  actor: TraceActor;
  actorLabel: string;
  eventTitle: string;
};

type TraceNode =
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

type TraceTurnGroup = {
  turnIndex: number;
  userPreview: string;
  entries: TraceNode[];
};

const MAX_DELEGATE_NESTING_DEPTH = 5;

type DelegateTraceMetadata = {
  agent_name?: string | null;
  delegated_by?: string | null;
  delegate_tool_name?: string | null;
  delegate_tool_call_id?: string | null;
  delegate_run_id?: string | null;
  parent_delegate_run_id?: string | null;
  status?: DelegateRunStatus | null;
  delegation_depth?: number | null;
  parent_iteration?: number | null;
};

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
    delegated_by: typeof record.delegated_by === "string" ? record.delegated_by : null,
    delegate_tool_name: typeof record.delegate_tool_name === "string" ? record.delegate_tool_name : null,
    delegate_tool_call_id: hasToolCallId ? toolCallId : null,
    delegate_run_id: hasRunId ? runId : null,
    parent_delegate_run_id: typeof record.parent_delegate_run_id === "string" ? record.parent_delegate_run_id : null,
    status: typeof record.status === "string" ? (record.status as DelegateRunStatus) : null,
    delegation_depth: typeof record.delegation_depth === "number" ? record.delegation_depth : null,
    parent_iteration: typeof record.parent_iteration === "number" ? record.parent_iteration : null,
  };
}

function readChildToolCallIdsFromPayload(payload: unknown): string[] {
  // A delegate_tool_calls event carries the child tool calls its subagent issued;
  // those child ids are the only way to reconstruct parent→child nesting because
  // each delegate event only carries its immediate parent's tool_call_id.
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
  // For each top-level position, either an event entry or a delegate id to expand.
  const topLevelPlan: Array<{ kind: "event"; entry: EnrichedTraceEntry } | { kind: "delegate"; id: string }> = [];
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
      result.push(buildDelegateNode(slot.id, bucketMeta.get(slot.id)!, buckets.get(slot.id)!, depth));
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
  const runShortId = meta.delegate_run_id ? meta.delegate_run_id.slice(0, 12) : null;
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

function getMaxTracePanelWidth(containerWidth: number): number {
  if (!Number.isFinite(containerWidth) || containerWidth <= 0) {
    return MAX_TRACE_PANEL_WIDTH;
  }
  return Math.max(MIN_TRACE_PANEL_WIDTH, Math.min(MAX_TRACE_PANEL_WIDTH, containerWidth - MIN_CONVERSATION_PANEL_WIDTH));
}

function clampTracePanelWidth(value: number, containerWidth: number): number {
  const maxWidth = getMaxTracePanelWidth(containerWidth);
  return Math.min(maxWidth, Math.max(MIN_TRACE_PANEL_WIDTH, Math.round(value)));
}

function buildAttachmentId(file: Pick<File, "name" | "size" | "lastModified">, deliveryMode?: AttachmentDeliveryMode): string {
  return `${file.name}-${file.size}-${file.lastModified}-${deliveryMode || "default"}`;
}

// Mirror of the backend TEXT_EXTENSIONS in src/covalent/core/attachment_processing.py.
const INLINE_PARSED_TEXT_EXTENSIONS = new Set([".txt", ".md", ".json", ".py", ".yaml", ".yml", ".csv", ".tsv"]);

function isInlineParsedByDefault(file: Pick<File, "name" | "type">): boolean {
  if (file.type.startsWith("image/") || file.type.startsWith("text/")) {
    return true;
  }
  const dot = file.name.lastIndexOf(".");
  const suffix = dot === -1 ? "" : file.name.slice(dot).toLowerCase();
  return INLINE_PARSED_TEXT_EXTENSIONS.has(suffix);
}

function normalizePastedImage(file: File, index: number): File {
  if (file.name.trim()) {
    return file;
  }
  const imageSubtype = file.type.startsWith("image/") ? file.type.slice("image/".length) || "png" : "png";
  return new File([file], `pasted-image-${Date.now()}-${index + 1}.${imageSubtype}`, {
    type: file.type || "image/png",
    lastModified: file.lastModified || Date.now(),
  });
}

function extractMarkdownText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map((child) => extractMarkdownText(child)).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractMarkdownText(node.props.children);
  }
  return "";
}

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();

  const successful = document.execCommand("copy");
  textarea.remove();

  if (!successful) {
    throw new Error("Copy command failed");
  }
}

function ChatBubbleCopy({ content }: { content: string }) {
  const copyResetRef = useRef<number | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied">("idle");

  useEffect(() => {
    return () => {
      if (copyResetRef.current !== null) {
        window.clearTimeout(copyResetRef.current);
      }
    };
  }, []);

  async function handleCopy(): Promise<void> {
    try {
      await copyText(content);
      setCopyState("copied");
    } catch {
      return;
    }
    if (copyResetRef.current !== null) {
      window.clearTimeout(copyResetRef.current);
    }
    copyResetRef.current = window.setTimeout(() => {
      setCopyState("idle");
      copyResetRef.current = null;
    }, CODE_BLOCK_COPY_RESET_MS);
  }

  return (
    <button
      className="chat-message-action"
      onClick={handleCopy}
      type="button"
      aria-label="Copy message"
      title={copyState === "copied" ? "Copied" : "Copy message"}
    >
      {copyState === "copied" ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}

function ChatCodeBlock({ children, tone, ...props }: ComponentPropsWithoutRef<"pre"> & { tone: ChatCodeBlockTone }) {
  const copyResetRef = useRef<number | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle");
  const codeChild = isValidElement<ComponentPropsWithoutRef<"code">>(children) ? children : null;
  const codeContent = codeChild?.props.children ?? children;
  const codeText = useMemo(() => extractMarkdownText(codeContent).replace(/\n$/, ""), [codeContent]);
  const isOutbound = tone === "outbound";
  const containerStyle: CSSProperties = {
    position: "relative",
    display: "block",
    width: "100%",
    maxWidth: "100%",
    minWidth: 0,
    overflow: "hidden",
    borderRadius: 12,
    background: isOutbound ? "rgba(255, 255, 255, 0.1)" : "var(--surface-tertiary)",
    boxShadow: isOutbound
      ? "inset 0 0 0 1px rgba(255, 255, 255, 0.12)"
      : "inset 0 0 0 1px color-mix(in oklch, var(--fg-primary), transparent 92%)",
  };
  const toolbarStyle: CSSProperties = {
    position: "absolute",
    top: 10,
    right: 10,
    zIndex: 1,
  };
  const copyButtonStyle: CSSProperties = {
    minHeight: 28,
    padding: "0 10px",
    borderRadius: 8,
    border: isOutbound ? "1px solid rgba(255, 255, 255, 0.18)" : "1px solid var(--border-soft)",
    background: isOutbound ? "rgba(255, 255, 255, 0.14)" : "var(--surface-primary)",
    color: isOutbound ? "var(--fg-inverse)" : "var(--fg-primary)",
    fontSize: 11,
    fontWeight: 700,
    lineHeight: 1,
    cursor: "pointer",
  };
  const preStyle: CSSProperties = {
    display: "block",
    width: "100%",
    maxWidth: "100%",
    minWidth: 0,
    margin: 0,
    overflowX: "auto",
    overflowY: "hidden",
    borderRadius: "inherit",
    padding: "44px 14px 14px",
    background: "transparent",
    boxShadow: "none",
    color: isOutbound ? "var(--fg-inverse)" : "var(--fg-primary)",
    fontFamily: "var(--font-mono), monospace",
    fontSize: 12,
    lineHeight: 1.5,
  };
  const codeStyle: CSSProperties = {
    display: "block",
    width: "max-content",
    minWidth: "100%",
    borderRadius: 0,
    padding: 0,
    background: "transparent",
    color: "inherit",
  };

  useEffect(() => {
    return () => {
      if (copyResetRef.current !== null) {
        window.clearTimeout(copyResetRef.current);
      }
    };
  }, []);

  async function handleCopy(): Promise<void> {
    if (!codeText) {
      return;
    }
    try {
      await copyText(codeText);
      setCopyState("copied");
    } catch {
      setCopyState("error");
    }

    if (copyResetRef.current !== null) {
      window.clearTimeout(copyResetRef.current);
    }
    copyResetRef.current = window.setTimeout(() => {
      setCopyState("idle");
      copyResetRef.current = null;
    }, CODE_BLOCK_COPY_RESET_MS);
  }

  return (
    <div className="chat-code-block" style={containerStyle}>
      <div className="chat-code-block-toolbar" style={toolbarStyle}>
        <button className="chat-code-block-copy" onClick={handleCopy} style={copyButtonStyle} type="button">
          {copyState === "copied" ? "Copied" : copyState === "error" ? "Retry copy" : "Copy"}
        </button>
      </div>
      <pre {...props} style={preStyle}>
        <code className={codeChild?.props.className} style={codeStyle}>
          {codeContent}
        </code>
      </pre>
    </div>
  );
}

function isWaitingForAnswerContent(content: string): boolean {
  const normalized = content.trim();
  return normalized === "Waiting for your answer…" || normalized === "Waiting for your answer...";
}

function ChatMarkdownContent({
  content,
  tone,
}: {
  content: string;
  tone: ChatCodeBlockTone;
}) {
  return (
    <div className="chat-markdown">
      <ReactMarkdown
        components={{
          pre({ children, ...props }) {
            return (
              <ChatCodeBlock {...props} tone={tone}>
                {children}
              </ChatCodeBlock>
            );
          },
        }}
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function AskUserPromptSummary({ prompt }: { prompt: PendingQuestionRequest }) {
  return (
    <section aria-label="Agent question" className="ask-user-prompt-summary">
      <div className="ask-user-prompt-summary-header">
        <p className="section-kicker">Input required</p>
        <strong className="ask-user-prompt-summary-title">{prompt.title}</strong>
      </div>
      <div className="ask-user-prompt-summary-list">
        {prompt.questions.map((question) => (
          <article className="ask-user-prompt-summary-card" key={question.header}>
            <strong>{question.header}</strong>
            <p>{question.question}</p>
            {question.message ? <p className="helper-copy">{question.message}</p> : null}
            {question.options.length ? (
              <ul className="ask-user-prompt-option-list">
                {question.options.map((option) => (
                  <li key={option.label}>
                    {option.label}
                    {option.description ? ` — ${option.description}` : ""}
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function ChatThinkingIndicator() {
  return (
    <div className="chat-thinking-indicator" role="status">
      <span>Thinking</span>
      <span aria-hidden="true" className="chat-thinking-dots">
        <span className="chat-thinking-dot" />
        <span className="chat-thinking-dot" />
        <span className="chat-thinking-dot" />
      </span>
    </div>
  );
}

function ChatReasoningBlock({ reasoning, active }: { reasoning: string; active: boolean }) {
  // null = 跟随自动策略（思考流式且正文为空时展开，正文开始后折叠）。
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? active;
  // 思考内容按来源分段：子代理段带名字 header，无标记的旧数据优雅降级。
  const segments = parseReasoningSegments(reasoning);
  return (
    <div className={`chat-reasoning-block${open ? " is-open" : ""}`}>
      <button
        aria-expanded={open}
        className="chat-reasoning-toggle"
        onClick={() => setManualOpen((current) => !(current ?? active))}
        type="button"
      >
        <span className="chat-reasoning-label">{active ? "Thinking…" : "Thought process"}</span>
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

function ChatMessageBubble({
  message,
  sending,
  messageTimestampFallback,
  editingMessageId,
  editingDraft,
  onEditStart,
  onEditChange,
  onEditCancel,
  onEditSubmit,
}: {
  message: Message;
  sending: boolean;
  messageTimestampFallback: number;
  editingMessageId: string | null;
  editingDraft: string;
  onEditStart: (message: Message) => void;
  onEditChange: (value: string) => void;
  onEditCancel: () => void;
  onEditSubmit: () => void;
}) {
  const tone = message.role === "user" ? "outbound" : "inbound";
  const isEditing = editingMessageId === message.id;
  const canEdit = message.role === "user" && !sending && !isEditing && !message.askUserPrompt;
  const displayContent =
    message.askUserPrompt && isWaitingForAnswerContent(message.content) ? "" : message.content;
  const hasReasoning = message.role === "assistant" && Boolean(message.reasoning);
  const isReasoningActive = sending && hasReasoning && !displayContent;
  const isThinking =
    sending && message.role === "assistant" && !displayContent && !message.askUserPrompt && !hasReasoning;
  const markdownContent = displayContent;
  const messageTimestamp = getTimestampFromId(message.id, messageTimestampFallback);

  const editContainerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
    width: "100%",
  };

  const editInputStyle: CSSProperties = {
    width: "100%",
    resize: "vertical",
    minHeight: "4rem",
    boxSizing: "border-box",
  };

  const editActionsStyle: CSSProperties = {
    display: "flex",
    gap: "0.5rem",
    justifyContent: "flex-end",
  };

  return (
    <article className={message.role === "user" ? "chat-message-row outbound" : "chat-message-row inbound"}>
      <div className={`chat-message-stack${isEditing ? " chat-message-stack-editing" : ""}`}>
        <div
          className={
            message.role === "user"
              ? `chat-bubble outbound${isEditing ? " chat-bubble-editing" : ""}`
              : "chat-bubble inbound"
          }
        >
          {hasReasoning ? <ChatReasoningBlock active={isReasoningActive} reasoning={message.reasoning || ""} /> : null}
          {message.askUserPrompt ? <AskUserPromptSummary prompt={message.askUserPrompt} /> : null}
          {message.attachments?.length ? (
            <div className="chat-attachment-list">
              {message.attachments.map((file) =>
                file.downloadUrl ? (
                  <a className="chat-attachment-chip chat-attachment-chip-link" download href={file.downloadUrl} key={file.id}>
                    <span className="chat-attachment-topline">
                      <strong>{file.name}</strong>
                      <span className="chat-attachment-badge">{formatAttachmentBadge(file)}</span>
                    </span>
                    <span className="chat-attachment-meta">{formatAttachmentMeta(file)}</span>
                    {file.summary ? <span className="chat-attachment-summary">{file.summary}</span> : null}
                    <span className="chat-attachment-action">Download</span>
                  </a>
                ) : (
                  <span className="chat-attachment-chip" key={file.id}>
                    <span className="chat-attachment-topline">
                      <strong>{file.name}</strong>
                      <span className="chat-attachment-badge">{formatAttachmentBadge(file)}</span>
                    </span>
                    <span className="chat-attachment-meta">{formatAttachmentMeta(file)}</span>
                    {file.summary ? <span className="chat-attachment-summary">{file.summary}</span> : null}
                  </span>
                ),
              )}
            </div>
          ) : null}
          {isEditing ? (
            <div className="chat-bubble-edit" style={editContainerStyle}>
              <textarea
                className="chat-bubble-edit-input"
                value={editingDraft}
                onChange={(event) => onEditChange(event.target.value)}
                rows={3}
                style={editInputStyle}
              />
              <div className="chat-bubble-edit-actions" style={editActionsStyle}>
                <button type="button" onClick={onEditCancel}>
                  Cancel
                </button>
                <button type="button" onClick={onEditSubmit} disabled={!editingDraft.trim()}>
                  Save &amp; resend
                </button>
              </div>
            </div>
          ) : isThinking ? (
            <ChatThinkingIndicator />
          ) : (
            <ChatMarkdownContent content={markdownContent} tone={tone} />
          )}
        </div>
        <div aria-label="Message actions" className="chat-message-actions" role="group">
          <time className="chat-message-time" dateTime={new Date(messageTimestamp).toISOString()}>
            {formatMessageTimestamp(messageTimestamp)}
          </time>
          <ChatBubbleCopy content={displayContent || ""} />
          {canEdit ? (
            <button
              className="chat-message-action"
              onClick={() => onEditStart(message)}
              type="button"
              aria-label="Edit message"
              title="Edit message"
            >
              <Pencil width={13} height={13} />
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function toUploadedComposerAttachment(file: AttachmentUploadItem): ComposerAttachment {
  return {
    id: buildAttachmentId({ name: file.name, size: file.size, lastModified: file.last_modified }, file.delivery_mode),
    name: file.name,
    size: file.size,
    type: file.content_type || "application/octet-stream",
    lastModified: file.last_modified,
    workspacePath: file.workspace_path,
    uploadedAt: file.uploaded_at,
    deliveryMode: file.delivery_mode,
    kind: file.kind,
    summary: file.summary,
    modelPromptText: file.model_prompt_text,
    modelContent: file.model_content,
    pageCount: file.page_count,
  };
}

function formatDeliveryModeLabel(mode?: AttachmentDeliveryMode): string | null {
  if (mode === "workspace") {
    return "workspace only";
  }
  if (mode === "parse") {
    return "inline parsed";
  }
  return null;
}

function formatAttachmentBadge(file: ComposerAttachment): string {
  if (file.kind) {
    return file.kind.toUpperCase();
  }
  if (file.type === "application/pdf") {
    return "PDF";
  }
  return (file.type.split("/")[0] || "file").toUpperCase();
}

function formatAttachmentMeta(file: ComposerAttachment): string {
  const parts = [formatFileSize(file.size)];
  if (typeof file.pageCount === "number") {
    parts.push(`${file.pageCount} pages`);
  }
  const deliveryLabel = formatDeliveryModeLabel(file.deliveryMode);
  if (deliveryLabel) {
    parts.push(deliveryLabel);
  }
  return parts.join(" • ");
}

function fallbackAttachmentPrompt(file: ComposerAttachment): string {
  const details = [file.kind || file.type || "binary", formatFileSize(file.size)];
  if (typeof file.pageCount === "number") {
    details.push(`${file.pageCount} pages`);
  }
  const lines = [`Attachment: ${file.name}`, `Kind: ${details.join(", ")}`];
  if (file.summary) {
    lines.push(`Summary: ${file.summary}`);
  }
  if (file.workspacePath) {
    lines.push(`Workspace path: ${file.workspacePath}`);
  }
  lines.push("Inspect the workspace file directly if you need more detail.");
  return lines.join("\n");
}

function buildRequestInput(prompt: string, attachments: ComposerAttachment[]): string | AgentInputPart[] {
  if (attachments.length === 0) {
    return prompt;
  }

  const parts: AgentInputPart[] = [];
  if (prompt) {
    parts.push({ type: "text", text: prompt });
  } else {
    const hasWorkspaceOnly = attachments.some((file) => file.deliveryMode === "workspace");
    const hasInlineParsed = attachments.some((file) => file.deliveryMode !== "workspace");
    const lead =
      hasWorkspaceOnly && hasInlineParsed
        ? "Use the uploaded attachments and inspect any workspace-only files directly before answering."
        : hasWorkspaceOnly
          ? "The user uploaded files to your workspace. Inspect those files directly and answer based on their contents."
          : "Analyze the uploaded attachments and answer based on their contents.";
    parts.push({ type: "text", text: lead });
  }

  for (const file of attachments) {
    const modelParts = Array.isArray(file.modelContent)
      ? file.modelContent.filter((item): item is AgentInputPart => Boolean(item && typeof item === "object"))
      : [];
    if (modelParts.length > 0) {
      parts.push(...modelParts);
      continue;
    }

    const promptText = file.modelPromptText?.trim();
    parts.push({ type: "text", text: promptText || fallbackAttachmentPrompt(file) });
  }

  return parts;
}

function formatAttachmentMemoryPrompt(attachments: ComposerAttachment[]): string {
  if (attachments.length === 0) {
    return "";
  }
  return [
    "Uploaded attachments summary:",
    ...attachments.map((file) => {
      const details = [file.kind || "binary", formatFileSize(file.size)];
      if (typeof file.pageCount === "number") {
        details.push(`${file.pageCount} pages`);
      }
      const deliveryLabel = formatDeliveryModeLabel(file.deliveryMode);
      if (deliveryLabel) {
        details.push(deliveryLabel);
      }
      const suffix = file.workspacePath ? ` [${file.workspacePath}]` : "";
      const summary = file.summary ? `: ${file.summary}` : "";
      return `- ${file.name} (${details.join(", ")})${summary}${suffix}`;
    }),
  ].join("\n");
}

function serializeAttachmentForMetadata(file: ComposerAttachment): Record<string, unknown> {
  return {
    id: file.id,
    name: file.name,
    size: file.size,
    type: file.type,
    lastModified: file.lastModified,
    workspacePath: file.workspacePath,
    uploadedAt: file.uploadedAt,
    deliveryMode: file.deliveryMode,
    kind: file.kind,
    summary: file.summary,
    pageCount: file.pageCount,
  };
}

function parseToolContentObject(raw: unknown): Record<string, unknown> | null {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw !== "string") {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function inferAttachmentKind(type: string): ComposerAttachment["kind"] {
  const normalized = type.trim().toLowerCase();
  if (normalized === "application/pdf") {
    return "pdf";
  }
  if (normalized.startsWith("image/")) {
    return "image";
  }
  if (normalized.startsWith("text/")) {
    return "text";
  }
  return "binary";
}

function mergeAttachments(current: ComposerAttachment[], incoming: ComposerAttachment[]): ComposerAttachment[] {
  if (incoming.length === 0) {
    return current;
  }
  const merged = new Map<string, ComposerAttachment>();
  for (const file of [...current, ...incoming]) {
    const key = file.id || file.downloadUrl || file.workspacePath || `${file.name}-${file.size}-${file.lastModified}`;
    merged.set(key, file);
  }
  return Array.from(merged.values());
}

function extractPublishedDownloadsFromPayload(payload: unknown): ComposerAttachment[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const results = Array.isArray((payload as { results?: unknown[] }).results) ? (payload as { results: unknown[] }).results : [];
  return results.flatMap((rawResult) => {
    if (!rawResult || typeof rawResult !== "object") {
      return [];
    }
    const result = rawResult as Record<string, unknown>;
    if (result.name !== "publish_downloadable_file" || result.is_error === true) {
      return [];
    }
    const content = parseToolContentObject(result.content);
    if (!content) {
      return [];
    }
    const name = typeof content.name === "string" ? content.name : "Download";
    const size = typeof content.size === "number" ? content.size : Number(content.size) || 0;
    const type = typeof content.content_type === "string" ? content.content_type : "application/octet-stream";
    const downloadUrl = typeof content.download_url === "string" ? content.download_url : undefined;
    if (!downloadUrl) {
      return [];
    }
    return [
      normalizeAttachment({
        id: typeof content.id === "string" ? content.id : `download-${name}-${size}`,
        name,
        size,
        type,
        content_type: type,
        last_modified: 0,
        workspace_path: content.workspace_path,
        download_url: downloadUrl,
        uploaded_at: content.published_at,
        summary: content.summary,
        kind: inferAttachmentKind(type),
      }),
    ];
  });
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function pickAvailableAgentName(agents: AgentDetail[], ...candidates: Array<string | null | undefined>): string {
  for (const candidate of candidates) {
    const normalized = candidate?.trim();
    if (!normalized) {
      continue;
    }
    // A chat session stores the agent's internal name while the list carries
    // the public display name (agents may have display_name != internal name,
    // e.g. "Skill Writer" for "default"). Match on either, and return the
    // public name so the selection stays consistent with the picker options.
    const matched = agents.find((agent) => agent.name === normalized || agent.internal_name === normalized);
    if (matched) {
      return matched.name;
    }
  }
  return agents[0]?.name || "";
}

function normalizePendingQuestionOption(raw: unknown) {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const value = raw as Record<string, unknown>;
  if (typeof value.label !== "string" || !value.label.trim()) {
    return null;
  }
  return {
    label: value.label,
    description: typeof value.description === "string" ? value.description : null,
    recommended: Boolean(value.recommended),
  };
}

function normalizePendingQuestionRequest(raw: unknown): PendingQuestionRequest | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }

  const value = raw as Record<string, unknown>;
  if (typeof value.id !== "string" || typeof value.tool_name !== "string") {
    return null;
  }

  const questions: PendingQuestionRequest["questions"] = Array.isArray(value.questions)
    ? value.questions
        .flatMap((item) => {
          if (!item || typeof item !== "object") {
            return [];
          }
          const question = item as Record<string, unknown>;
          if (typeof question.header !== "string" || typeof question.question !== "string") {
            return [];
          }
          const normalizedQuestion: PendingQuestionRequest["questions"][number] = {
            header: question.header,
            question: question.question,
            message: typeof question.message === "string" ? question.message : null,
            multi_select: Boolean(question.multi_select),
            allow_freeform_input: question.allow_freeform_input !== false,
            max_selections:
              typeof question.max_selections === "number"
                ? question.max_selections
                : Number(question.max_selections) || null,
            options: Array.isArray(question.options)
              ? question.options
                  .map((option) => normalizePendingQuestionOption(option))
                  .filter((option): option is NonNullable<ReturnType<typeof normalizePendingQuestionOption>> => Boolean(option))
              : [],
          };
          return [normalizedQuestion];
        })
    : [];

  if (questions.length === 0) {
    return null;
  }

  return {
    id: value.id,
    tool_call_id: typeof value.tool_call_id === "string" ? value.tool_call_id : null,
    tool_name: value.tool_name,
    title: typeof value.title === "string" && value.title.trim() ? value.title : "Additional input required",
    questions,
  };
}

function getResolvedQuestionsFromActivity(items: ActivityItem[]): PendingQuestionRequest[] {
  const resolvedIds = new Set<string>();

  for (const item of items) {
    if (item.title === "input_resolved" && item.payload && typeof item.payload === "object") {
      const resolvedId = (item.payload as Record<string, unknown>).id;
      if (typeof resolvedId === "string" && resolvedId.trim()) {
        resolvedIds.add(resolvedId);
      }
    }
  }

  const resolvedQuestions: PendingQuestionRequest[] = [];
  for (const item of items) {
    if (item.title !== "input_required") {
      continue;
    }
    const request = normalizePendingQuestionRequest(item.payload);
    if (request && resolvedIds.has(request.id)) {
      resolvedQuestions.push(request);
    }
  }

  return resolvedQuestions;
}

function enrichMessagesWithAskUserPrompts(
  messages: ChatThreadMessage[],
  activity: ActivityItem[],
): ChatThreadMessage[] {
  const resolvedQuestions = getResolvedQuestionsFromActivity(activity);
  if (resolvedQuestions.length === 0) {
    return messages.map((message) =>
      message.role === "assistant" && isWaitingForAnswerContent(message.content)
        ? { ...message, content: "" }
        : message,
    );
  }

  let questionIdx = 0;
  const enriched: ChatThreadMessage[] = [];

  for (const message of messages) {
    if (message.role === "user" && questionIdx < resolvedQuestions.length) {
      const hasPriorUser = enriched.some((entry) => entry.role === "user");
      if (hasPriorUser) {
        const request = resolvedQuestions[questionIdx];
        enriched.push({
          id: `ask-user-prompt-${request.id}`,
          role: "assistant",
          content: "",
          askUserPrompt: request,
        });
        questionIdx += 1;
      }
    }

    if (
      message.role === "assistant" &&
      isWaitingForAnswerContent(message.content) &&
      !message.askUserPrompt
    ) {
      enriched.push({ ...message, content: "" });
      continue;
    }

    enriched.push(message);
  }

  return enriched;
}

function getPendingQuestionFromActivity(items: ActivityItem[]): PendingQuestionRequest | null {
  const resolvedIds = new Set<string>();

  for (const item of [...items].reverse()) {
    if (item.title === "input_resolved" && item.payload && typeof item.payload === "object") {
      const resolvedId = (item.payload as Record<string, unknown>).id;
      if (typeof resolvedId === "string" && resolvedId.trim()) {
        resolvedIds.add(resolvedId);
      }
      continue;
    }
    if (item.title !== "input_required") {
      continue;
    }
    const request = normalizePendingQuestionRequest(item.payload);
    if (request && !resolvedIds.has(request.id)) {
      return request;
    }
  }

  return null;
}

function buildInitialPendingDrafts(request: PendingQuestionRequest | null): Record<string, PendingQuestionDraft> {
  if (!request) {
    return {};
  }
  return Object.fromEntries(
    request.questions.map((question) => {
      const recommended = question.options.filter((option) => option.recommended).map((option) => option.label);
      const selectedOptions = question.multi_select ? recommended : recommended.slice(0, 1);
      return [question.header, { selectedOptions, freeform: "" }];
    }),
  );
}

function formatPendingAnswerValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  return typeof value === "string" ? value : String(value ?? "");
}

function extractPendingAnswers(
  request: PendingQuestionRequest,
  drafts: Record<string, PendingQuestionDraft>,
): { answers: Record<string, string | string[]>; summary: string } | null {
  const entries: Array<[string, string | string[]]> = [];

  for (const question of request.questions) {
    const draft = drafts[question.header] || { selectedOptions: [], freeform: "" };
    const freeform = draft.freeform.trim();
    let value: string | string[] = "";

    if (question.options.length === 0) {
      value = freeform;
    } else if (question.multi_select) {
      const combined = Array.from(new Set([...draft.selectedOptions, ...(freeform ? [freeform] : [])]));
      if (question.max_selections && combined.length > question.max_selections) {
        return null;
      }
      value = combined;
    } else {
      value = freeform || draft.selectedOptions[0] || "";
    }

    if ((Array.isArray(value) && value.length === 0) || (!Array.isArray(value) && !String(value).trim())) {
      return null;
    }

    entries.push([question.header, value]);
  }

  return {
    answers: Object.fromEntries(entries),
    summary: entries.map(([header, value]) => `${header}: ${formatPendingAnswerValue(value)}`).join("\n"),
  };
}

function toTimestamp(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Date.now();
}

function normalizeAttachment(raw: Record<string, unknown>): ComposerAttachment {
  const explicitId = typeof raw.id === "string" && raw.id.trim() ? raw.id : null;
  const name = typeof raw.name === "string" ? raw.name : "Attachment";
  const sizeValue = raw.size;
  const size = typeof sizeValue === "number" ? sizeValue : Number(sizeValue) || 0;
  const type =
    typeof raw.type === "string"
      ? raw.type
      : typeof raw.content_type === "string"
        ? raw.content_type
        : "application/octet-stream";
  const lastModifiedValue = raw.lastModified ?? raw.last_modified;
  const lastModified = typeof lastModifiedValue === "number" ? lastModifiedValue : Number(lastModifiedValue) || 0;
  const workspacePath =
    typeof raw.workspacePath === "string"
      ? raw.workspacePath
      : typeof raw.workspace_path === "string"
        ? raw.workspace_path
        : undefined;
  const downloadUrl =
    typeof raw.downloadUrl === "string"
      ? raw.downloadUrl
      : typeof raw.download_url === "string"
        ? raw.download_url
        : undefined;
  const uploadedAt =
    typeof raw.uploadedAt === "string"
      ? raw.uploadedAt
      : typeof raw.uploaded_at === "string"
        ? raw.uploaded_at
        : undefined;
  const deliveryMode =
    raw.deliveryMode === "parse" || raw.deliveryMode === "workspace"
      ? raw.deliveryMode
      : raw.delivery_mode === "parse" || raw.delivery_mode === "workspace"
        ? raw.delivery_mode
        : undefined;
  const kind =
    raw.kind === "text" || raw.kind === "image" || raw.kind === "pdf" || raw.kind === "binary"
      ? raw.kind
      : undefined;
  const summary = typeof raw.summary === "string" ? raw.summary : undefined;
  const pageCountValue = raw.pageCount ?? raw.page_count;
  const pageCount = typeof pageCountValue === "number" ? pageCountValue : Number(pageCountValue);
  return {
    id: explicitId || buildAttachmentId({ name, size, lastModified }, deliveryMode),
    name,
    size,
    type,
    lastModified,
    workspacePath,
    downloadUrl,
    uploadedAt,
    deliveryMode,
    kind,
    summary,
    pageCount: Number.isFinite(pageCount) ? pageCount : undefined,
  };
}

function chatThreadMessageFromSession(message: ChatSessionMessage): ChatThreadMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    reasoning: message.reasoning_content || undefined,
    attachments: message.attachments.map((attachment) => normalizeAttachment(attachment)),
    position: message.position ?? null,
  };
}

function threadFromSession(session: ChatSession): ChatThread {
  const activity = session.activity.map((item) => ({
    id: item.id,
    title: item.title,
    payload: item.payload,
    hasRawRequest: item.has_raw_request ?? false,
    hasRawResponse: item.has_raw_response ?? false,
  }));
  const baseMessages: ChatThreadMessage[] = session.messages.map(chatThreadMessageFromSession);

  return {
    id: session.id,
    title: session.title,
    titleSource: session.title_source,
    sessionId: session.id,
    agentName: session.agent_name || "",
    messages: enrichMessagesWithAskUserPrompts(baseMessages, activity),
    activity,
    createdAt: toTimestamp(session.created_at),
    updatedAt: toTimestamp(session.updated_at),
    previewText: session.preview_text,
    isLoaded: true,
    isPersisted: true,
    pendingQuestion: getPendingQuestionFromActivity(session.activity.map((item) => ({ id: item.id, title: item.title, payload: item.payload }))),
    contextTruncated: false,
    compactionMethod: null,
    messagesTotal: session.messages_total,
    messagesHasMore: session.messages_has_more ?? false,
  };
}

function getTimestampFromId(value: string, fallback: number): number {
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

function formatMessageTimestamp(value: number): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(value);
}

function asTracePayloadRecord(payload: unknown): Record<string, unknown> | null {
  return payload && typeof payload === "object" && !Array.isArray(payload) ? (payload as Record<string, unknown>) : null;
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

function formatTracePayload(payload: unknown): string {
  return typeof payload === "string" ? payload : `${JSON.stringify(payload, null, 2)}\n`;
}

function getTraceDisplayPayload(title: string, payload: unknown): unknown {
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

function getModelCallRawPayload(payload: unknown, panel: ModelRawPanel): unknown | null {
  const record = asTracePayloadRecord(payload);
  if (!record) {
    return null;
  }
  const rawPayload = panel === "request" ? record.raw_request : record.raw_response;
  return rawPayload === undefined || rawPayload === null ? null : rawPayload;
}

function buildSyntheticMessageTraceEntries(
  messages: Message[],
  activityItems: ActivityItem[] = [],
): ActivityItem[] {
  const syntheticItems: ActivityItem[] = messages
    .filter((message) => message.role === "user")
    .map((message) => ({
      id: `${message.id}-trace`,
      title: "user_message",
      payload: {
        text: message.content,
        attachment_count: message.attachments?.length || 0,
        source: message.askUserPrompt ? "ask_user_response" : "chat_query",
      },
    }));

  const hasRuntimeFinal = activityItems.some((item) => getBaseEventTitle(item.title) === "final");
  if (hasRuntimeFinal) {
    return syntheticItems;
  }

  const latestAssistantMessage = [...messages]
    .reverse()
    .find(
      (message) =>
        message.role === "assistant" &&
        message.content.trim().length > 0 &&
        !message.askUserPrompt,
    );
  if (!latestAssistantMessage) {
    return syntheticItems;
  }

  const latestActivityTimestamp = activityItems.reduce(
    (latest, item) => Math.max(latest, getTimestampFromId(item.id, 0)),
    0,
  );
  const assistantTimestamp = getTimestampFromId(latestAssistantMessage.id, 0);
  const finalTimestamp = Math.max(latestActivityTimestamp + 1, assistantTimestamp);

  return [
    ...syntheticItems,
    {
      id: `final-${finalTimestamp}-synthetic`,
      title: "final",
      payload: {
        output_text: latestAssistantMessage.content,
        source: "conversation_message",
      },
    },
  ];
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
  if (baseTitle === "assistant" || baseTitle === "final" || baseTitle === "tool_calls") {
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

function getTraceEventLabel(title: string, rawPayload?: unknown): string {
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

function getTurnUserPreview(message: Message): string {
  const content = message.content.trim();
  if (content) {
    return truncateTraceSummaryText(content, 96);
  }
  const attachmentNames = (message.attachments || [])
    .map((attachment) => attachment.name.trim())
    .filter(Boolean);
  if (attachmentNames.length === 1) {
    return truncateTraceSummaryText(attachmentNames[0], 96);
  }
  if (attachmentNames.length > 1) {
    return `${attachmentNames.length} attachments`;
  }
  return "User message";
}

function buildTraceTurnGroups(
  messages: Message[],
  items: EnrichedTraceEntry[],
): TraceTurnGroup[] {
  if (items.length === 0) {
    return [];
  }

  const userMessages = messages.filter((message) => message.role === "user");
  if (userMessages.length === 0) {
    return [
      {
        turnIndex: 1,
        userPreview: "Initial run",
        entries: buildTraceTree(items),
      },
    ];
  }

  const groups: Array<{ turnIndex: number; userPreview: string; items: EnrichedTraceEntry[] }> = userMessages.map(
    (message, index) => ({
      turnIndex: index + 1,
      userPreview: getTurnUserPreview(message),
      items: [],
    }),
  );

  for (const item of items) {
    const itemTimestamp = getTimestampFromId(item.id, 0);
    let assignedIndex = 0;
    for (let index = 0; index < userMessages.length; index += 1) {
      const userTimestamp = getTimestampFromId(userMessages[index].id, 0);
      if (userTimestamp <= itemTimestamp) {
        assignedIndex = index;
      }
    }
    groups[assignedIndex]?.items.push(item);
  }

  return groups
    .filter((group) => group.items.length > 0)
    .map((group) => ({
      turnIndex: group.turnIndex,
      userPreview: group.userPreview,
      entries: buildTraceTree(group.items),
    }));
}

const traceRawDetailCache = new Map<string, Promise<{ payload: unknown }>>();

function fetchTraceRawDetail(sessionId: string, activityId: string): Promise<{ payload: unknown }> {
  const cacheKey = `${sessionId}:${activityId}`;
  let promise = traceRawDetailCache.get(cacheKey);
  if (!promise) {
    promise = getChatSessionActivity(sessionId, activityId);
    traceRawDetailCache.set(cacheKey, promise);
    promise.catch(() => traceRawDetailCache.delete(cacheKey));
  }
  return promise;
}

function TraceStepEntry({
  entry: item,
  summary,
  sessionId,
}: {
  entry: EnrichedTraceEntry;
  summary: string | null;
  sessionId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [rawPanel, setRawPanel] = useState<ModelRawPanel | null>(null);
  const [fetchedRaw, setFetchedRaw] = useState<Partial<Record<ModelRawPanel, unknown>>>({});
  const [rawLoading, setRawLoading] = useState(false);
  const [rawFetchFailed, setRawFetchFailed] = useState(false);
  const rawFetchStartedRef = useRef(false);
  const isModelCall = getBaseEventTitle(item.title) === "model_call";
  const payloadRequest = getModelCallRawPayload(item.payload, "request");
  const payloadResponse = getModelCallRawPayload(item.payload, "response");
  const hasServerRaw = Boolean(item.hasRawRequest || item.hasRawResponse);
  // Raw blobs live server-side only; fetch them once when the step is expanded.
  useEffect(() => {
    if (!expanded || !isModelCall || !hasServerRaw || !sessionId || rawFetchFailed || rawFetchStartedRef.current) {
      return;
    }
    if (payloadRequest !== null && payloadResponse !== null) {
      return;
    }
    rawFetchStartedRef.current = true;
    let active = true;
    setRawLoading(true);
    fetchTraceRawDetail(sessionId, item.id)
      .then((detail) => {
        if (!active) {
          return;
        }
        const record = asTracePayloadRecord(detail.payload);
        setFetchedRaw((current) => ({
          ...current,
          request: record && record.raw_request !== undefined ? record.raw_request : current.request,
          response: record && record.raw_response !== undefined ? record.raw_response : current.response,
        }));
      })
      .catch(() => {
        if (active) {
          setRawFetchFailed(true);
        }
      })
      .finally(() => {
        if (active) {
          setRawLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [expanded, hasServerRaw, isModelCall, item.id, payloadRequest, payloadResponse, rawFetchFailed, sessionId]);
  const rawRequest =
    item.hasRawRequest === false ? null : (payloadRequest ?? fetchedRaw.request ?? null);
  const rawResponse =
    item.hasRawResponse === false ? null : (payloadResponse ?? fetchedRaw.response ?? null);
  const visiblePayload = getTraceDisplayPayload(item.title, item.payload);
  const payloadText = formatTracePayload(visiblePayload);
  const rawPayload = rawPanel === "request" ? rawRequest : rawPanel === "response" ? rawResponse : null;
  const rawPayloadText = rawPayload === null ? "" : formatTracePayload(rawPayload);
  const isLongPayload = payloadText.length > TRACE_PAYLOAD_PREVIEW_CHARS;
  const displayPayload =
    isLongPayload && !expanded
      ? `${payloadText.slice(0, TRACE_PAYLOAD_PREVIEW_CHARS).trimEnd()}...`
      : payloadText;

  return (
    <div className={cn("trace-step", `is-actor-${item.actor}`, expanded && "is-expanded")}>
      <div className="trace-step-line flex h-6 min-h-6 items-center gap-2 overflow-hidden px-2">
        <span className={cn("trace-step-actor shrink-0", `is-${item.actor}`)}>
          {item.actorLabel}
        </span>
        <span className="trace-step-event w-[52px] shrink-0 truncate">{item.label}</span>
        <span className="trace-step-summary min-w-0 flex-1 truncate">
          {summary || item.eventTitle}
        </span>
        <span className="trace-step-time w-14 shrink-0 text-right">{item.displayTime}</span>
        <button
          className="trace-step-payload-toggle w-14 shrink-0 truncate text-right"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {expanded ? "hide" : "payload"}
        </button>
      </div>
      {expanded ? (
        <div className="trace-step-details">
          <pre className="trace-step-payload">{displayPayload}</pre>
          {isModelCall && rawLoading ? (
            <p className="trace-step-raw-loading">Loading raw payload…</p>
          ) : null}
          {isModelCall && rawFetchFailed && hasServerRaw ? (
            <p className="trace-step-raw-loading">Raw payload unavailable.</p>
          ) : null}
          {isModelCall && (rawRequest !== null || rawResponse !== null) ? (
            <div className="trace-step-raw">
              <div className="trace-step-raw-controls" aria-label="Model call raw payload controls">
                {rawRequest !== null ? (
                  <button
                    className={cn("trace-step-raw-toggle", rawPanel === "request" && "is-active")}
                    onClick={() => setRawPanel((current) => current === "request" ? null : "request")}
                    type="button"
                  >
                    Raw request
                  </button>
                ) : null}
                {rawResponse !== null ? (
                  <button
                    className={cn("trace-step-raw-toggle", rawPanel === "response" && "is-active")}
                    onClick={() => setRawPanel((current) => current === "response" ? null : "response")}
                    type="button"
                  >
                    Raw response
                  </button>
                ) : null}
              </div>
              {rawPayload !== null ? <pre className="trace-step-payload trace-step-raw-payload">{rawPayloadText}</pre> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function truncateTraceSummaryText(value: string, maxChars = 240): string {
  const normalized = value.trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(maxChars - 3, 1)).trimEnd()}...`;
}

function TraceNodeList({ nodes, sessionId }: { nodes: TraceNode[]; sessionId: string }) {
  return (
    <>
      {nodes.map((node) => {
        if (node.kind === "event") {
          return (
            <TraceStepEntry
              entry={node.entry}
              key={node.entry.id}
              summary={getTraceSummary(node.entry)}
              sessionId={sessionId}
            />
          );
        }
        return <TraceDelegateGroup key={`delegate-${node.firstItemId}`} node={node} sessionId={sessionId} />;
      })}
    </>
  );
}

function TraceDelegateGroup({
  node,
  sessionId,
}: {
  node: Extract<TraceNode, { kind: "delegate" }>;
  sessionId: string;
}) {
  // Count children for the badge (event-kind children only; nested delegate
  // groups count as 1 each for display purposes).
  const eventCount = node.children.length;
  const [expanded, setExpanded] = useState(false);
  const sourceLabel = node.agentName
    ? node.delegatedBy
      ? `${node.agentName} via ${node.delegatedBy}`
      : node.agentName
    : "subagent";
  const indentStyle = { marginLeft: `${Math.min(node.depth - 1, MAX_DELEGATE_NESTING_DEPTH - 1) * 12}px` };

  return (
    <div className="trace-delegate-group" style={indentStyle}>
      <button
        aria-expanded={expanded}
        className="trace-step trace-delegate-header"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <span className="trace-step-line">
          <span className="trace-step-actor shrink-0 is-subagent">Subagent</span>
          <span className="trace-step-event w-[52px] shrink-0 truncate">subagent</span>
          <span className="trace-step-summary min-w-0 flex-1 truncate">
            {sourceLabel} · {eventCount} event{eventCount === 1 ? "" : "s"}
            {node.runShortId ? ` · ${node.runShortId}` : ""}
          </span>
          <span className="trace-step-time w-14 shrink-0 text-right">depth {node.depth}</span>
          <span className="trace-step-payload-toggle w-14 shrink-0 truncate text-right">
            {expanded ? "hide" : "show"}
          </span>
        </span>
      </button>
      {expanded ? (
        <div className="trace-delegate-children">
          <TraceNodeList nodes={node.children} sessionId={sessionId} />
        </div>
      ) : null}
    </div>
  );
}

const DELEGATE_EVENT_PREFIX = "delegate_";

// Base titles (prefix-stripped) of the stateful delegate lifecycle events.
// They are accepted as trace stream events — and given lifecycle labels and
// summaries — only when the full title carries the delegate_ prefix, so a
// hypothetical root event literally named "created" or "idle" never matches.
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

function isDelegateEventTitle(value: string): boolean {
  return value.startsWith(DELEGATE_EVENT_PREFIX);
}

function getBaseEventTitle(value: string): string {
  return isDelegateEventTitle(value) ? value.slice(DELEGATE_EVENT_PREFIX.length) : value;
}

function withTraceSourcePrefix(isDelegate: boolean, payload: Record<string, unknown>, summary: string): string {
  if (!isDelegate) {
    return summary;
  }

  const agentName = typeof payload.agent_name === "string" ? payload.agent_name.trim() : "";
  if (!agentName) {
    return `Subagent: ${summary}`;
  }

  const delegatedBy = typeof payload.delegated_by === "string" ? payload.delegated_by.trim() : "";
  const sourceLabel = delegatedBy ? `${agentName} via ${delegatedBy}` : agentName;
  return `Subagent ${sourceLabel}: ${summary}`;
}

function getTraceSummary(item: ActivityItem): string | null {
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
    const method = typeof payload.compaction_method === "string" ? payload.compaction_method : "";
    const estimatedTokens = Number(payload.estimated_prompt_tokens) || 0;
    const tokenBudget = Number(payload.token_budget) || 0;
    const tokenInfo = estimatedTokens && tokenBudget ? ` ~${formatCompactNumber(estimatedTokens)}/${formatCompactNumber(tokenBudget)} tokens` : "";
    const methodLabel = method === "summarize" ? "LLM summarized" : method === "prune+summarize" ? "pruned + LLM summarized" : method === "prune" ? "pruned" : "compacted";
    return withTraceSourcePrefix(
      isDelegate,
      payload,
      `${phase} ${methodLabel} ${originalMessages} to ${requestMessages} messages${tokenInfo}. Summarized ${summarized}, dropped ${dropped}, truncated ${truncated}.`,
    );
  }

  if (baseTitle === "model_call") {
    const provider = typeof payload.provider === "string" ? payload.provider : "provider";
    const model = typeof payload.model === "string" ? payload.model : "model";
    const phase = typeof payload.phase === "string" ? payload.phase : "react";
    const status = payload.status === "error" ? "failed" : "completed";
    const duration = formatDurationLabel(payload.elapsed_ms);
    const requestMessages = Number(payload.request_message_count) || 0;
    const promptTokens = Number(payload.prompt_tokens) || 0;
    const completionTokens = Number(payload.completion_tokens) || 0;
    const tokenInfo = promptTokens ? ` (${formatCompactNumber(promptTokens)}+${formatCompactNumber(completionTokens)} tokens)` : "";
    const detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
    const base = `${provider} / ${model} ${phase} call ${status}${duration ? ` in ${duration}` : ""} with ${requestMessages} messages${tokenInfo}.`;
    return withTraceSourcePrefix(isDelegate, payload, detail ? `${base} ${detail}` : base);
  }

  if (baseTitle === "tool_calls") {
    const toolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls.length : 0;
    if (!toolCalls) {
      return isDelegate ? withTraceSourcePrefix(isDelegate, payload, "Requested tool execution.") : null;
    }
    return withTraceSourcePrefix(isDelegate, payload, `Requested ${toolCalls} tool call${toolCalls === 1 ? "" : "s"}.`);
  }

  if (baseTitle === "tool_results") {
    const results = Array.isArray(payload.results) ? payload.results.length : 0;
    if (!results) {
      return isDelegate ? withTraceSourcePrefix(isDelegate, payload, "Collected tool results.") : null;
    }
    return withTraceSourcePrefix(isDelegate, payload, `Collected ${results} tool result${results === 1 ? "" : "s"}.`);
  }

  if (baseTitle === "thought") {
    const summary = typeof payload.summary === "string" ? payload.summary.trim() : "";
    return summary ? withTraceSourcePrefix(isDelegate, payload, summary) : null;
  }

  if (baseTitle === "iteration") {
    const iteration = Number(payload.iteration) || 0;
    const base = iteration ? `Started ReAct iteration ${iteration}.` : "Started a ReAct iteration.";
    return withTraceSourcePrefix(isDelegate, payload, base);
  }

  if (baseTitle === "assistant") {
    const text = typeof payload.text === "string" ? payload.text.trim() : "";
    return text ? withTraceSourcePrefix(isDelegate, payload, `Streaming response: ${truncateTraceSummaryText(text)}`) : null;
  }

  if (baseTitle === "final") {
    const text = typeof payload.output_text === "string" ? payload.output_text.trim() : "";
    return text
      ? withTraceSourcePrefix(isDelegate, payload, `Completed with final response: ${truncateTraceSummaryText(text)}`)
      : withTraceSourcePrefix(isDelegate, payload, "Completed with final response.");
  }

  if (baseTitle === "input_required") {
    const title = typeof payload.title === "string" ? payload.title.trim() : "";
    return withTraceSourcePrefix(isDelegate, payload, title ? `Paused for input: ${title}` : "Paused for input.");
  }

  if (baseTitle === "error") {
    const detail = typeof payload.detail === "string" ? payload.detail.trim() : "";
    return detail ? withTraceSourcePrefix(isDelegate, payload, detail) : null;
  }

  if (isDelegate && DELEGATE_LIFECYCLE_BASE_TITLES.has(baseTitle)) {
    const summary = typeof payload.summary === "string" ? payload.summary.trim() : "";
    if (baseTitle === "waiting_parent") {
      return withTraceSourcePrefix(
        isDelegate,
        payload,
        summary ? `Waiting for parent: ${truncateTraceSummaryText(summary)}` : "Waiting for parent.",
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
        baseTitle === "released" ? "Released" : baseTitle === "cancelled" ? "Cancelled" : baseTitle === "expired" ? "Expired" : "Failed";
      return withTraceSourcePrefix(
        isDelegate,
        payload,
        summary ? `${terminalLabel}: ${truncateTraceSummaryText(summary)}` : `${terminalLabel}.`,
      );
    }
    // created / running / resumed carry no dedicated summary; the entry falls
    // back to its event title.
    return null;
  }

  return null;
}

function isTraceStreamEvent(event: string): boolean {
  const baseEvent = getBaseEventTitle(event);
  if (event.startsWith(DELEGATE_EVENT_PREFIX) && DELEGATE_LIFECYCLE_BASE_TITLES.has(baseEvent)) {
    return true;
  }
  return [
    "assistant",
    "final",
    "tool_calls",
    "tool_results",
    "iteration",
    "thought",
    "context_window",
    "model_call",
    "input_required",
    "error",
  ].includes(baseEvent);
}

export function ChatWorkspace() {
  const { user } = useAuth();
  const {
    threads,
    activeThreadId,
    activeThread,
    handleDeleteThread,
    updateThread,
    upsertThread,
    applySessionSummary,
  } = useChatSessions();

  const [, setHealth] = useState<HealthResponse | null>(null);
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [input, setInput] = useState("");
  const [draftAttachments, setDraftAttachments] = useState<ComposerAttachment[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [attachmentDeliveryMode, setAttachmentDeliveryMode] = useState<AttachmentDeliveryMode>("workspace");
  const [attachmentDeliveryModeTouched, setAttachmentDeliveryModeTouched] = useState(false);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [composerMultiline, setComposerMultiline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRenamingTitle, setIsRenamingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [pendingDrafts, setPendingDrafts] = useState<Record<string, PendingQuestionDraft>>({});
  const [tracePanelWidth, setTracePanelWidth] = useState(DEFAULT_TRACE_PANEL_WIDTH);
  const [isTracePanelOpen, setIsTracePanelOpen] = useState(true);
  const [isTraceResizing, setIsTraceResizing] = useState(false);
  const defaultAgentName = user?.preferences.default_agent?.trim() || "";
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const chatSplitRef = useRef<HTMLDivElement | null>(null);
  const skipLayoutPersistRef = useRef(true);
  const defaultAgentNameRef = useRef(defaultAgentName);

  useEffect(() => {
    defaultAgentNameRef.current = defaultAgentName;
  }, [defaultAgentName]);

  // Tracks the in-flight agent run. A new run (or unmount) aborts the previous
  // one so its stream can no longer mutate thread state.
  const activeRunRef = useRef<{ id: number; controller: AbortController } | null>(null);
  // The backend-side durable run backing the active stream. Unlike the SSE
  // view (activeRunRef), this run keeps executing when the view disconnects —
  // it must be cancelled explicitly before a new turn starts.
  const activeBackendRunRef = useRef<{ runId: string; agentName: string } | null>(null);
  const undoInProgressRef = useRef(false);
  // Transcript pagination: guards concurrent older-message fetches and lets the
  // post-commit effect restore the viewport after prepending (scroll anchoring).
  const loadOlderInFlightRef = useRef(false);
  const pendingScrollAnchorRef = useRef<{ threadId: string; previousHeight: number; previousFirstId: string | null } | null>(null);
  const pendingScrollToBottomRef = useRef<string | null>(null);
  const messageStageRef = useRef<HTMLDivElement | null>(null);

  // Snapshot used by Task 8's undo toast: the original edited message and the
  // tail that was discarded when the user resent an edited message. Cleared on
  // error and after undo is consumed.
  const editUndoRef = useRef<{
    threadId: string;
    sessionId: string;
    originalMessage: Message;
    removedTail: Message[];
    prefix: Message[];
  } | null>(null);
  const editUndoToastRef = useRef<string | number | null>(null);

  const clearEditUndo = useCallback(() => {
    editUndoRef.current = null;
    if (editUndoToastRef.current !== null) {
      toast.dismiss(editUndoToastRef.current);
      editUndoToastRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      // Abort any in-flight stream when the component unmounts.
      activeRunRef.current?.controller.abort();
      activeRunRef.current = null;
      clearEditUndo();
    };
  }, [clearEditUndo]);

  // Clear the undo slot when the active thread changes so a stale snapshot
  // from one thread can't be restored into another.
  useEffect(() => {
    clearEditUndo();
  }, [activeThreadId, clearEditUndo]);

  useEffect(() => {
    const traceStored = window.localStorage.getItem(TRACE_PANEL_VISIBLE_STORAGE_KEY);
    if (traceStored === "0") {
      setIsTracePanelOpen(false);
    }

    const storedWidth = window.localStorage.getItem(TRACE_PANEL_STORAGE_KEY);
    if (storedWidth) {
      const parsed = Number(storedWidth);
      if (Number.isFinite(parsed)) {
        setTracePanelWidth(parsed);
      }
    }

    skipLayoutPersistRef.current = false;
  }, []);

  useEffect(() => {
    if (skipLayoutPersistRef.current) {
      return;
    }
    window.localStorage.setItem(TRACE_PANEL_VISIBLE_STORAGE_KEY, isTracePanelOpen ? "1" : "0");
  }, [isTracePanelOpen]);

  useEffect(() => {
    if (skipLayoutPersistRef.current) {
      return;
    }
    window.localStorage.setItem(TRACE_PANEL_STORAGE_KEY, `${tracePanelWidth}`);
  }, [tracePanelWidth]);

  useEffect(() => {
    const splitLayout = chatSplitRef.current;
    if (!splitLayout || typeof ResizeObserver === "undefined") {
      return;
    }

    const syncWidth = (containerWidth: number) => {
      setTracePanelWidth((current) => clampTracePanelWidth(current, containerWidth));
    };

    syncWidth(splitLayout.clientWidth);

    const observer = new ResizeObserver((entries) => {
      const nextWidth = entries[0]?.contentRect.width ?? splitLayout.clientWidth;
      syncWidth(nextWidth);
    });

    observer.observe(splitLayout);
    return () => {
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [healthResult, agentResult] = await Promise.all([getHealth(), getAgents()]);
        if (cancelled) {
          return;
        }
        const sortedAgents = sortAgentsForPicker(agentResult);
        setHealth(healthResult);
        setAgents(sortedAgents);
        setSelectedAgent((current) =>
          pickAvailableAgentName(sortedAgents, defaultAgentNameRef.current, current, sortedAgents[0]?.name),
        );
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load agents.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setSelectedAgent((current) =>
      pickAvailableAgentName(agents, activeThread?.agentName, defaultAgentName, current, threads[0]?.agentName),
    );
  }, [activeThread?.agentName, activeThread?.id, agents, defaultAgentName, threads]);

  useEffect(() => {
    setIsRenamingTitle(false);
    setTitleDraft(activeThread?.title || "");
  }, [activeThread?.id, activeThread?.title]);

  useEffect(() => {
    let cancelled = false;

    async function hydrateSession() {
      if (!activeThread || !activeThread.isPersisted || activeThread.isLoaded) {
        return;
      }

      try {
        const session = await getChatSession(activeThread.sessionId, { messagesLimit: MESSAGES_INITIAL_LIMIT });
        if (cancelled) {
          return;
        }
        const hydratedThread = threadFromSession(session);
        upsertThread(hydratedThread);
        pendingScrollToBottomRef.current = hydratedThread.id;
        setSelectedAgent((current) => pickAvailableAgentName(agents, hydratedThread.agentName, defaultAgentName, current));
        // If this session still has a background run executing (the user
        // navigated away and came back), reattach and replay its events.
        try {
          const runAgentName = hydratedThread.agentName || defaultAgentName;
          const runs = await listAgentRuns(runAgentName, activeThread.sessionId);
          const openRun = runs.find((candidate) => candidate.status === "running" || candidate.status === "cancelling");
          if (openRun && !cancelled && !activeRunRef.current) {
            await reattachToRun(hydratedThread, runAgentName, openRun.id);
          }
        } catch {
          // Runs listing unavailable — the session simply renders its persisted state.
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "Failed to load conversation.");
        }
      }
    }

    void hydrateSession();
    return () => {
      cancelled = true;
    };
  }, [activeThread, agents, defaultAgentName, upsertThread]);

  // Pages in the next batch of older messages when the stage is scrolled near
  // the top; the post-commit effect below restores the viewport after prepend.
  async function loadOlderMessages() {
    const thread = activeThread;
    if (!thread || thread.messagesHasMore !== true || loadOlderInFlightRef.current) {
      return;
    }
    const firstPosition = thread.messages[0]?.position;
    if (typeof firstPosition !== "number") {
      return;
    }
    loadOlderInFlightRef.current = true;
    const stage = messageStageRef.current;
    pendingScrollAnchorRef.current = {
      threadId: thread.id,
      previousHeight: stage?.scrollHeight ?? 0,
      previousFirstId: thread.messages[0]?.id ?? null,
    };
    updateThread(thread.id, (current) => ({ ...current, isLoadingOlderMessages: true }));
    try {
      const session = await getChatSession(thread.sessionId, {
        messagesLimit: MESSAGES_OLDER_PAGE_SIZE,
        messagesBefore: firstPosition,
      });
      // Set just before the prepend lands: an earlier render (the loading
      // flag) would consume the flag before the new groups exist.
      shouldRecollapseTraceRef.current = true;
      updateThread(thread.id, (current) => {
        const known = new Set(current.messages.map((message) => message.id));
        const older = session.messages
          .filter((message) => !known.has(message.id))
          .map(chatThreadMessageFromSession);
        return {
          ...current,
          messages: [...older, ...current.messages],
          messagesHasMore: session.messages_has_more ?? false,
          messagesTotal: session.messages_total,
          isLoadingOlderMessages: false,
        };
      });
    } catch (loadError) {
      pendingScrollAnchorRef.current = null;
      updateThread(thread.id, (current) => ({ ...current, isLoadingOlderMessages: false }));
      setError(loadError instanceof Error ? loadError.message : "Failed to load earlier messages.");
    } finally {
      loadOlderInFlightRef.current = false;
    }
  }

  function handleMessageStageScroll() {
    const stage = messageStageRef.current;
    if (!stage) {
      return;
    }
    if (stage.scrollTop > MESSAGE_STAGE_TOP_TRIGGER_PX) {
      return;
    }
    // Not actually scrollable (short transcript) — loading older on a
    // top-pinned stage would fetch the whole history pointlessly.
    if (stage.scrollHeight <= stage.clientHeight + MESSAGE_STAGE_TOP_TRIGGER_PX) {
      return;
    }
    void loadOlderMessages();
  }

  // Edit/undo rewrite the whole transcript server-side, so a partially loaded
  // thread must be fully fetched first or the replace would drop history.
  async function ensureFullTranscriptLoaded(): Promise<ChatThreadMessage[] | null> {
    const thread = activeThread;
    if (!thread) {
      return null;
    }
    if (thread.messagesHasMore !== true) {
      return thread.messages;
    }
    try {
      const session = await getChatSession(thread.sessionId, { messagesLimit: MESSAGES_FULL_LOAD_LIMIT });
      const full = session.messages.map(chatThreadMessageFromSession);
      updateThread(thread.id, (current) => ({
        ...current,
        messages: full,
        messagesHasMore: false,
        messagesTotal: session.messages_total,
      }));
      return full;
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load conversation history.");
      return null;
    }
  }

  // Post-commit viewport fixes: after hydration jump to the newest message;
  // after prepending older ones, compensate the grown scrollHeight so the
  // viewport stays anchored on the same content instead of jumping.
  useEffect(() => {
    const stage = messageStageRef.current;
    if (!stage || !activeThread) {
      return;
    }
    if (pendingScrollToBottomRef.current === activeThread.id) {
      pendingScrollToBottomRef.current = null;
      stage.scrollTop = stage.scrollHeight;
      return;
    }
    const anchor = pendingScrollAnchorRef.current;
    if (
      anchor &&
      anchor.threadId === activeThread.id &&
      activeThread.messages[0]?.id !== anchor.previousFirstId
    ) {
      pendingScrollAnchorRef.current = null;
      stage.scrollTop += stage.scrollHeight - anchor.previousHeight;
    }
  }, [activeThread]);

  useEffect(() => {
    setInput("");
    setDraftAttachments([]);
    setError(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, [activeThreadId]);

  useEffect(() => {
    const textarea = composerRef.current;
    if (!textarea) {
      return;
    }

    textarea.style.height = "0px";
    const computedStyle = window.getComputedStyle(textarea);
    const lineHeight = Number.parseFloat(computedStyle.lineHeight) || 18;
    const paddingTop = Number.parseFloat(computedStyle.paddingTop) || 0;
    const paddingBottom = Number.parseFloat(computedStyle.paddingBottom) || 0;
    const borderTop = Number.parseFloat(computedStyle.borderTopWidth) || 0;
    const borderBottom = Number.parseFloat(computedStyle.borderBottomWidth) || 0;
    const singleLineHeight = Math.ceil(lineHeight + paddingTop + paddingBottom + borderTop + borderBottom);
    const maxHeight = lineHeight * 5 + paddingTop + paddingBottom + borderTop + borderBottom;

    if (!input.trim()) {
      textarea.style.height = `${singleLineHeight}px`;
      textarea.style.overflowY = "hidden";
      setComposerMultiline(false);
      return;
    }

    const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
    const isMultiline = textarea.scrollHeight > singleLineHeight + 2;

    textarea.style.height = `${Math.max(nextHeight, singleLineHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
    setComposerMultiline(isMultiline);
  }, [input]);

  const currentAgent = useMemo(
    () => agents.find((agent) => agent.name === selectedAgent) ?? null,
    [agents, selectedAgent],
  );

  const attachmentDrafts = draftAttachments;
  const conversationMessages = useMemo(
    () => (activeThread?.messages || []) as Message[],
    [activeThread?.messages],
  );

  const traceEntries = useMemo(() => {
    const activityItems = activeThread?.activity || [];
    const syntheticItems = buildSyntheticMessageTraceEntries(
      conversationMessages,
      activityItems,
    );
    const items = [...activityItems, ...syntheticItems].sort(
      (left, right) =>
        getTimestampFromId(left.id, activeThread?.updatedAt || Date.now()) -
        getTimestampFromId(right.id, activeThread?.updatedAt || Date.now()),
    );
    return items.map((item) => {
      const timestamp = getTimestampFromId(item.id, activeThread?.updatedAt || Date.now());
      const actor = getTraceActor(item.title, item.payload);
      return {
        ...item,
        label: getTraceEventLabel(item.title, item.payload),
        displayTime: formatTime(timestamp),
        actor,
        actorLabel: getTraceActorLabel(actor),
        eventTitle: formatActivityTitle(getBaseEventTitle(item.title)),
      };
    });
  }, [activeThread?.activity, activeThread?.updatedAt, conversationMessages]);

  const traceTurnGroups = useMemo(
    () => buildTraceTurnGroups(conversationMessages, traceEntries),
    [conversationMessages, traceEntries],
  );
  const [collapsedTraceTurns, setCollapsedTraceTurns] = useState<Set<number>>(
    () => new Set(),
  );
  // Entering a session starts with every turn collapsed except the newest —
  // full DOM expansion of long histories is a major render cost. Manual
  // toggles after init are preserved; switching threads re-initializes.
  // Lazily prepended history re-applies the same rule (flag below) so the
  // freshly revealed old turns render collapsed too.
  const traceCollapseInitRef = useRef<{ threadId: string } | null>(null);
  const shouldRecollapseTraceRef = useRef(false);
  useEffect(() => {
    if (!activeThread || traceTurnGroups.length === 0) {
      return;
    }
    const init = traceCollapseInitRef.current;
    if (init?.threadId === activeThread.id && !shouldRecollapseTraceRef.current) {
      return;
    }
    traceCollapseInitRef.current = { threadId: activeThread.id };
    shouldRecollapseTraceRef.current = false;
    setCollapsedTraceTurns(
      new Set(traceTurnGroups.slice(0, -1).map((turn) => turn.turnIndex)),
    );
  }, [activeThread, traceTurnGroups]);
  const displayedTraceEntries = traceEntries;
  const activePendingQuestion = activeThread?.pendingQuestion || null;
  const chatSplitStyle = {
    "--chat-trace-width": `${tracePanelWidth}px`,
  } as CSSProperties;

  useEffect(() => {
    setPendingDrafts(buildInitialPendingDrafts(activePendingQuestion));
  }, [activePendingQuestion]);

  function toggleTraceTurn(turnIndex: number) {
    setCollapsedTraceTurns((current) => {
      const next = new Set(current);
      if (next.has(turnIndex)) {
        next.delete(turnIndex);
      } else {
        next.add(turnIndex);
      }
      return next;
    });
  }

  function handleTraceResizeStart(event: React.MouseEvent<HTMLDivElement>) {
    if (!isTracePanelOpen || event.button !== 0 || window.matchMedia("(max-width: 980px)").matches) {
      return;
    }

    const splitLayout = chatSplitRef.current;
    if (!splitLayout) {
      return;
    }

    event.preventDefault();
    event.currentTarget.focus();

    const startX = event.clientX;
    const startWidth = tracePanelWidth;
    const rootStyle = document.documentElement.style;
    const previousCursor = rootStyle.cursor;
    const previousUserSelect = rootStyle.userSelect;

    setIsTraceResizing(true);
    rootStyle.cursor = "col-resize";
    rootStyle.userSelect = "none";

    const handlePointerMove = (moveEvent: MouseEvent) => {
      const delta = startX - moveEvent.clientX;
      setTracePanelWidth(clampTracePanelWidth(startWidth + delta, splitLayout.clientWidth));
    };

    const stopResizing = () => {
      setIsTraceResizing(false);
      rootStyle.cursor = previousCursor;
      rootStyle.userSelect = previousUserSelect;
      window.removeEventListener("mousemove", handlePointerMove);
      window.removeEventListener("mouseup", stopResizing);
    };

    window.addEventListener("mousemove", handlePointerMove);
    window.addEventListener("mouseup", stopResizing);
  }

  function handleTraceResizeKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!isTracePanelOpen || window.matchMedia("(max-width: 980px)").matches) {
      return;
    }

    const splitLayout = chatSplitRef.current;
    if (!splitLayout) {
      return;
    }

    const maxWidth = getMaxTracePanelWidth(splitLayout.clientWidth);
    let nextWidth: number | null = null;

    if (event.key === "ArrowLeft") {
      nextWidth = tracePanelWidth + 24;
    } else if (event.key === "ArrowRight") {
      nextWidth = tracePanelWidth - 24;
    } else if (event.key === "Home") {
      nextWidth = MIN_TRACE_PANEL_WIDTH;
    } else if (event.key === "End") {
      nextWidth = maxWidth;
    }

    if (nextWidth === null) {
      return;
    }

    event.preventDefault();
    setTracePanelWidth(clampTracePanelWidth(nextWidth, splitLayout.clientWidth));
  }

  function resolveFileDeliveryMode(file: File): AttachmentDeliveryMode {
    if (attachmentDeliveryModeTouched) {
      return attachmentDeliveryMode;
    }
    return isInlineParsedByDefault(file) ? "parse" : "workspace";
  }

  async function queueComposerFiles(incomingFiles: File[]) {
    if (incomingFiles.length === 0 || !activeThread) {
      return;
    }

    const fileModes = new Map(incomingFiles.map((file) => [file, resolveFileDeliveryMode(file)]));
    const knownIds = new Set(draftAttachments.map((file) => file.id));
    const uniqueFiles = incomingFiles.filter((file) => !knownIds.has(buildAttachmentId(file, fileModes.get(file))));
    if (uniqueFiles.length === 0) {
      return;
    }

    setError(null);
    setUploadingAttachments(true);
    try {
      const modeGroups = new Map<AttachmentDeliveryMode, File[]>();
      for (const file of uniqueFiles) {
        const mode = fileModes.get(file) as AttachmentDeliveryMode;
        const group = modeGroups.get(mode) || [];
        group.push(file);
        modeGroups.set(mode, group);
      }
      const uploadedFiles: AttachmentUploadItem[] = [];
      for (const [mode, files] of modeGroups) {
        const response = await uploadChatAttachments(activeThread.sessionId, files, mode);
        uploadedFiles.push(...response.files);
      }
      const uploaded = uploadedFiles.map((file) => toUploadedComposerAttachment(file));
      setDraftAttachments((current) => {
        const existing = new Set(current.map((file) => file.id));
        return [...current, ...uploaded.filter((file) => !existing.has(file.id))];
      });
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : "Failed to process attachments.");
    } finally {
      setUploadingAttachments(false);
    }
  }

  async function handleFileSelection(event: React.ChangeEvent<HTMLInputElement>) {
    const incomingFiles = Array.from(event.target.files || []);
    event.target.value = "";
    await queueComposerFiles(incomingFiles);
  }

  async function handleComposerPaste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
    const pastedImages = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => Boolean(file))
      .map((file, index) => normalizePastedImage(file, index));
    if (pastedImages.length === 0) {
      return;
    }
    event.preventDefault();
    await queueComposerFiles(pastedImages);
  }

  function handleRemoveFile(fileId: string) {
    setDraftAttachments((current) => current.filter((file) => file.id !== fileId));
  }

  async function handleSaveTitle() {
    if (!activeThread) {
      return;
    }
    const nextTitle = titleDraft.trim();
    if (!nextTitle) {
      return;
    }

    setError(null);
    try {
      if (activeThread.isPersisted) {
        const session = await renameChatSession(activeThread.sessionId, nextTitle);
        upsertThread(threadFromSession(session));
      } else {
        updateThread(activeThread.id, (thread) => ({ ...thread, title: nextTitle, titleSource: "manual" }));
      }
      setIsRenamingTitle(false);
    } catch (renameError) {
      setError(renameError instanceof Error ? renameError.message : "Failed to rename conversation.");
    }
  }

  async function handleDeleteActiveThread() {
    if (!activeThread) {
      return;
    }

    setError(null);
    try {
      await handleDeleteThread(activeThread.id, selectedAgent);
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Failed to delete conversation.");
    }
  }

  // Shared SSE-event applier for one assistant turn: appends token deltas,
  // reconciles full-text events, and flags termination (final / error /
  // input_required). Used by both the live send path and background-run
  // reattachment so replayed events render identically.
  function createRunEventApplier(threadId: string, assistantId: string) {
    let currentAssistantIteration: number | null = null;
    let deltasStreamedThisIteration = false;
    const state = { terminated: false };
    // 思考片段按 token 到达（一次长思考可达上万条），逐条 setState 会击穿
    // React 更新深度保护；这里累积后按帧合并刷新。缓冲内嵌来源标记
    // （见 reasoning-segments.ts）：子代理片段写入 agent 名，与后端
    // reasoning_content 的持久化格式一致，刷新后归因不丢。
    let reasoningBuffer = "";
    let reasoningActiveSource: string | null | undefined;
    let reasoningFlushScheduled = false;
    const flushReasoning = () => {
      reasoningFlushScheduled = false;
      if (!reasoningBuffer) {
        return;
      }
      const text = reasoningBuffer;
      reasoningBuffer = "";
      updateThread(threadId, (thread) => ({
        ...thread,
        updatedAt: Date.now(),
        messages: thread.messages.map((message) =>
          message.id === assistantId
            ? { ...message, reasoning: `${message.reasoning || ""}${text}` }
            : message,
        ),
      }));
    };
    const scheduleReasoningFlush = () => {
      if (reasoningFlushScheduled) {
        return;
      }
      reasoningFlushScheduled = true;
      if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") {
        window.requestAnimationFrame(flushReasoning);
      } else {
        setTimeout(flushReasoning, 16);
      }
    };
    const apply = (event: string, payload: unknown) => {
      // 非思考事件先同步冲刷缓冲，保证顺序与终态不丢片段。
      if (event !== "reasoning_delta" && event !== "delegate_reasoning_delta") {
        flushReasoning();
      }
      if (event === "assistant_delta") {
        const text = (payload as { text?: string })?.text || "";
        if (!text) {
          return;
        }
        // Token-level fragments: append within one iteration, but a new
        // iteration's first delta replaces the prior iteration's text.
        const iteration = (payload as { iteration?: number })?.iteration ?? 0;
        const isNewIteration = iteration !== currentAssistantIteration;
        currentAssistantIteration = iteration;
        deltasStreamedThisIteration = true;
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          messages: thread.messages.map((message) =>
            message.id === assistantId
              ? { ...message, content: isNewIteration ? text : `${message.content}${text}` }
              : message,
          ),
        }));
        return;
      }

      if (event === "reasoning_delta" || event === "delegate_reasoning_delta") {
        // 思考片段（原生推理或 <think> 前奏剥离；delegate_ 前缀来自子代理）：
        // 按到达顺序累积，按帧合并刷新（见 flushReasoning）。来源切换时
        // 先写入分段标记，前端与持久化侧据此显示署名。
        const record = payload as { text?: string; agent_name?: string };
        const text = record?.text || "";
        if (!text) {
          return;
        }
        const source =
          event === "delegate_reasoning_delta" && record?.agent_name ? String(record.agent_name) : null;
        if (reasoningActiveSource !== source) {
          reasoningBuffer += serializeReasoningMarker(source);
          reasoningActiveSource = source;
        }
        reasoningBuffer += text;
        scheduleReasoningFlush();
        return;
      }

      if (event === "assistant") {
        const text = (payload as { text?: string })?.text || "";
        const iteration = (payload as { iteration?: number })?.iteration ?? 0;
        // When the backend already streamed this iteration token-by-token
        // (assistant_delta), the full text is a duplicate — skip it.
        if (iteration === currentAssistantIteration && deltasStreamedThisIteration) {
          return;
        }
        deltasStreamedThisIteration = false;
        const isNewIteration = iteration !== currentAssistantIteration;
        currentAssistantIteration = iteration;
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          messages: thread.messages.map((message) =>
            message.id === assistantId
              ? { ...message, content: isNewIteration ? text : `${message.content}${text}` }
              : message,
          ),
        }));
        return;
      }

      if (event === "final") {
        state.terminated = true;
        const text = (payload as { output_text?: string })?.output_text || "";
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          pendingQuestion: null,
          messages: thread.messages.map((message) =>
            message.id === assistantId ? { ...message, content: text || message.content } : message,
          ),
        }));
        return;
      }

      if (event === "cancelled") {
        state.terminated = true;
        return;
      }

      if (event === "session") {
        const sessionSummary = payload as ChatSessionSummary;
        applySessionSummary(sessionSummary, threadId);
        return;
      }

      // Contract: only the root input_required opens the answer form; delegate_waiting_parent is trace-only and never matches this exact-name check.
      if (event === "input_required") {
        state.terminated = true;
        const pendingQuestion = normalizePendingQuestionRequest(payload);
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          pendingQuestion,
          activity: [...thread.activity, stripActivityItem({ id: uid(event), title: event, payload })],
          messages: thread.messages.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  content: message.content || "Waiting for your answer…",
                  askUserPrompt: pendingQuestion,
                }
              : message,
          ),
        }));
        return;
      }

      if (event === "error") {
        state.terminated = true;
        const detail =
          (payload as { detail?: string })?.detail ||
          (payload as { message?: string })?.message ||
          "Agent run failed.";
        setError(detail);
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          activity: [...thread.activity, stripActivityItem({ id: uid(event), title: event, payload })],
          messages: thread.messages.map((message) =>
            message.id === assistantId
              ? { ...message, content: message.content || detail }
              : message,
          ),
        }));
        return;
      }

      if (getBaseEventTitle(event) === "context_window") {
        const ctxPayload = payload as { truncated_message_count?: number; compaction_method?: string; summarized_message_count?: number };
        const truncated = Number(ctxPayload.truncated_message_count) || 0;
        const summarized = Number(ctxPayload.summarized_message_count) || 0;
        const method = typeof ctxPayload.compaction_method === "string" ? ctxPayload.compaction_method : null;
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          contextTruncated: (truncated > 0 || summarized > 0) ? true : thread.contextTruncated,
          compactionMethod: method || thread.compactionMethod,
          activity: [...thread.activity, stripActivityItem({ id: uid(event), title: event, payload })],
        }));
        return;
      }

      if (isTraceStreamEvent(event)) {
        const publishedDownloads = getBaseEventTitle(event) === "tool_results" ? extractPublishedDownloadsFromPayload(payload) : [];
        updateThread(threadId, (thread) => ({
          ...thread,
          updatedAt: Date.now(),
          activity: [...thread.activity, stripActivityItem({ id: uid(event), title: event, payload })],
          messages: publishedDownloads.length
            ? thread.messages.map((message) =>
                message.id === assistantId
                  ? { ...message, attachments: mergeAttachments((message.attachments || []) as ComposerAttachment[], publishedDownloads) }
                  : message,
              )
            : thread.messages,
        }));
        return;
      }
    };
    return {
      apply,
      get terminated() {
        return state.terminated;
      },
    };
  }

  // Cancels the live backend run (if any) and waits until the backend marks it
  // terminal, so the next POST /runs doesn't trip the one-run-per-session 409.
  async function cancelActiveBackendRun(sessionId: string | undefined) {
    const active = activeBackendRunRef.current;
    if (!active) {
      return;
    }
    try {
      await cancelAgentRun(active.agentName, active.runId);
    } catch {
      // Already terminal or unknown — nothing to wait for.
    }
    for (let attempt = 0; attempt < 20; attempt++) {
      if (!sessionId) {
        break;
      }
      try {
        const runs = await listAgentRuns(active.agentName, sessionId);
        const run = runs.find((candidate) => candidate.id === active.runId);
        if (!run || (run.status !== "running" && run.status !== "cancelling")) {
          break;
        }
      } catch {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    activeBackendRunRef.current = null;
  }

  async function handleStopRun() {
    const active = activeBackendRunRef.current;
    if (active) {
      try {
        await cancelAgentRun(active.agentName, active.runId);
      } catch {
        // The SSE view will still drain any terminal event already logged.
      }
    }
    activeRunRef.current?.controller.abort();
  }

  // Reattach to a still-running background run after a page change/remount:
  // rebuilds the turn from the run's full event replay (position 0).
  async function reattachToRun(thread: ChatThread, agentName: string, runId: string) {
    const threadId = thread.id;
    const assistantId = uid("assistant");
    setSending(true);
    const runController = new AbortController();
    const guardId = (activeRunRef.current?.id ?? 0) + 1;
    activeRunRef.current = { id: guardId, controller: runController };
    activeBackendRunRef.current = { runId, agentName };
    updateThread(threadId, (current) => ({
      ...current,
      isLoaded: true,
      messages: [...current.messages, { id: assistantId, role: "assistant", content: "" }],
    }));
    const applier = createRunEventApplier(threadId, assistantId);
    try {
      await streamAgentRunEvents(agentName, runId, {
        after: 0,
        onChunk: ({ event, payload }) => {
          if (event === "run_started") {
            const started = payload as { display_input?: string; user_message_id?: string };
            const userId = started.user_message_id || uid("user");
            updateThread(threadId, (current) =>
              current.messages.some((message) => message.id === userId)
                ? current
                : {
                    ...current,
                    messages: [...current.messages, { id: userId, role: "user", content: started.display_input || "" }],
                  },
            );
            return;
          }
          applier.apply(event, payload);
        },
        signal: runController.signal,
      });
    } catch {
      // View-level failure (abort/network): the backend run keeps executing;
      // the session will reflect the result when it finishes.
    } finally {
      if (activeRunRef.current?.id === guardId) {
        activeBackendRunRef.current = null;
        setSending(false);
      }
    }
  }

  async function runThreadRequest(params: {
    thread: ChatThread;
    requestInput: string | AgentInputPart[];
    userContent: string;
    attachments?: ComposerAttachment[];
    metadata?: Record<string, unknown>;
    clearPendingQuestion?: boolean;
    preserveEditUndo?: boolean;
  }) {
    if (undoInProgressRef.current) {
      return;
    }
    const attachments = params.attachments || [];
    const threadId = params.thread.id;
    const userMessageId = uid("user");
    const assistantId = uid("assistant");
    const userMessage: Message = { id: userMessageId, role: "user", content: params.userContent, attachments };

    // Any normal follow-up makes an older edit snapshot unsafe: Undo would
    // otherwise replace the transcript and discard this newer turn. The
    // edit-and-resend run is the sole caller allowed to keep its new snapshot.
    if (!params.preserveEditUndo) {
      clearEditUndo();
    }

    setError(null);
    setSending(true);

    // Abort any previously in-flight run before starting a new one. The guard
    // token (runId) lets callbacks from an already-aborted run detect that they
    // are stale and skip state writes.
    activeRunRef.current?.controller.abort();
    const runController = new AbortController();
    const runId = (activeRunRef.current?.id ?? 0) + 1;
    activeRunRef.current = { id: runId, controller: runController };
    let streamTerminatedCleanly = false;

    updateThread(threadId, (thread) => ({
      ...thread,
      title:
        thread.messages.length === 0 && thread.titleSource !== "manual"
          ? (params.userContent || attachments[0]?.name || thread.title).slice(0, 44)
          : thread.title,
      agentName: selectedAgent,
      updatedAt: Date.now(),
      previewText: params.userContent,
      isLoaded: true,
      pendingQuestion: params.clearPendingQuestion ? null : thread.pendingQuestion,
      messages: [...thread.messages, userMessage, { id: assistantId, role: "assistant", content: "" }],
    }));

    const applier = createRunEventApplier(threadId, assistantId);

    try {
      // The backend allows one live run per session: cancel any leftover
      // background run from a previous turn (or another tab) first.
      await cancelActiveBackendRun(params.thread.sessionId);
      const handle = await startAgentRun(
        selectedAgent,
        {
          input: params.requestInput,
          session_id: params.thread.sessionId,
          metadata: {
            source: "chat-workspace",
            display_input: params.userContent,
            user_message_id: userMessageId,
            ...(attachments.length ? { attachments: attachments.map((attachment) => serializeAttachmentForMetadata(attachment)) } : {}),
            ...(params.metadata || {}),
          },
        },
      );
      activeBackendRunRef.current = { runId: handle.run_id, agentName: selectedAgent };
      await streamAgentRunEvents(selectedAgent, handle.run_id, {
        onChunk: ({ event, payload }) => {
          applier.apply(event, payload);
          if (applier.terminated) {
            streamTerminatedCleanly = true;
          }
        },
        signal: runController.signal,
      });

      if (!streamTerminatedCleanly) {
        // If this run was superseded (a newer run started) or aborted (unmount /
        // navigation), do not overwrite the assistant bubble with an error — the
        // newer run (or the unmounted state) owns the UI now.
        const isStale = activeRunRef.current?.id !== runId;
        const wasAborted = runController.signal.aborted;
        if (!isStale && !wasAborted) {
          const detail = "Agent stream ended unexpectedly before a final response.";
          setError(detail);
          updateThread(threadId, (thread) => ({
            ...thread,
            messages: thread.messages.map((message) =>
              message.id === assistantId
                ? { ...message, content: message.content || detail }
                : message,
            ),
          }));
        }
      }
    } catch (sendError) {
      const isStale = activeRunRef.current?.id !== runId;
      const wasAborted = sendError instanceof StreamAbortedError || runController.signal.aborted;
      // Aborts are intentional (new run / unmount / navigation). Stale runs must
      // not touch state — the current run owns it.
      if (isStale || wasAborted) {
        return;
      }
      setError(sendError instanceof Error ? sendError.message : "Failed to run agent.");
      updateThread(threadId, (thread) => ({
        ...thread,
        pendingQuestion: params.clearPendingQuestion ? params.thread.pendingQuestion : thread.pendingQuestion,
        messages: thread.messages.map((message) =>
          message.id === assistantId
            ? { ...message, content: sendError instanceof Error ? sendError.message : "Agent run failed." }
            : message,
        ),
      }));
    } finally {
      // Only release the "sending" lock if this run is still the active one;
      // a newer run is managing its own lock.
      if (activeRunRef.current?.id === runId) {
        activeBackendRunRef.current = null;
        setSending(false);
      }
    }
  }

  // Restores the transcript snapshot stashed by handleEditResend: the original
  // edited message plus the tail that was discarded. Server-side replace then
  // local update; clears the undo slot on success.
  async function handleUndoEdit() {
    if (sending || undoInProgressRef.current) {
      return;
    }
    const snapshot = editUndoRef.current;
    if (!snapshot) {
      return;
    }
    const { threadId, sessionId, originalMessage, removedTail, prefix } = snapshot;
    const restored = [...prefix, originalMessage, ...removedTail];
    // Consume first so rapid double-clicks cannot issue competing replacements.
    undoInProgressRef.current = true;
    setSending(true);
    clearEditUndo();
    try {
      await replaceChatTranscript(sessionId, {
        messages: restored.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          attachments: (m.attachments ?? []) as unknown[],
        })),
      });
      updateThread(threadId, (t) => ({
        ...t,
        messages: restored as unknown as ChatThread["messages"],
        updatedAt: Date.now(),
      }));
    } catch (undoError) {
      setError(undoError instanceof Error ? undoError.message : "Failed to undo edit.");
      editUndoRef.current = snapshot;
      showEditUndoToast();
      return;
    } finally {
      undoInProgressRef.current = false;
      setSending(false);
    }
  }

  function showEditUndoToast() {
    if (!editUndoRef.current) {
      return;
    }
    if (editUndoToastRef.current !== null) {
      toast.dismiss(editUndoToastRef.current);
    }
    editUndoToastRef.current = toast("Edited message and resent.", {
      action: {
        label: "Undo",
        onClick: () => {
          void handleUndoEdit();
        },
      },
      duration: 8000,
    });
  }

  // Edit-and-resend: truncate the transcript at the edited user message
  // (server-side then local), then re-enter the run loop with the edited text
  // and the message's original attachments. `editUndoRef` stashes a snapshot
  // for the undo toast above.
  async function handleEditResend(message: Message, newContent: string) {
    if (!currentAgent || sending || !activeThread || !newContent.trim()) {
      return;
    }

    const thread = activeThread;
    let snapshotMessages = (thread.messages) as Message[];
    if (thread.messagesHasMore === true) {
      const full = await ensureFullTranscriptLoaded();
      if (!full) {
        return;
      }
      snapshotMessages = full as Message[];
    }
    const messageIndex = snapshotMessages.findIndex((m) => m.id === message.id);
    if (messageIndex === -1) {
      return;
    }

    // Snapshot for undo: original message + everything strictly after it.
    clearEditUndo();
    const originalMessage = message;
    const removedTail = snapshotMessages.slice(messageIndex + 1);
    const prefix = snapshotMessages.slice(0, messageIndex);
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
        clearEditUndo();
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
      preserveEditUndo: true,
    });

    // 4. Offer undo (Task 8 surfaces the toast; here we just flag completion).
    showEditUndoToast();
  }

  async function handleSend() {
    if (!currentAgent || sending || uploadingAttachments || !activeThread || (!input.trim() && attachmentDrafts.length === 0)) {
      return;
    }

    const prompt = input.trim();
    const attachments = [...attachmentDrafts];
    const attachmentMemoryPrompt = formatAttachmentMemoryPrompt(attachments);
    const requestInput = buildRequestInput(prompt, attachments);
    const hasWorkspaceOnly = attachments.some((file) => file.deliveryMode === "workspace");
    const hasInlineParsed = attachments.some((file) => file.deliveryMode !== "workspace");
    const defaultAttachmentContent =
      hasWorkspaceOnly && hasInlineParsed
        ? "Shared uploaded files and workspace file references."
        : hasWorkspaceOnly
          ? "Shared uploaded files in the agent workspace."
          : "Shared uploaded files.";
    const userContent = prompt || (attachments.length ? defaultAttachmentContent : "Shared selected file metadata.");
    const memoryUserInput = [prompt, attachmentMemoryPrompt].filter(Boolean).join("\n\n") || userContent;

    setInput("");
    setDraftAttachments([]);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }

    await runThreadRequest({
      thread: activeThread,
      requestInput,
      userContent,
      attachments,
      metadata: {
        memory_user_input: memoryUserInput,
      },
    });
  }

  async function handleSubmitPendingQuestion() {
    if (!currentAgent || sending || !activeThread || !activePendingQuestion) {
      return;
    }

    const response = extractPendingAnswers(activePendingQuestion, pendingDrafts);
    if (!response) {
      setError("Answer each required question before continuing.");
      return;
    }

    await runThreadRequest({
      thread: activeThread,
      requestInput: response.summary,
      userContent: response.summary,
      metadata: {
        resume_question_id: activePendingQuestion.id,
        question_response: response.answers,
      },
      clearPendingQuestion: true,
    });
  }

  return (
    <div className="workspace-shell chat-page-shell flex min-h-0 flex-1 flex-col overflow-hidden">
      {error ? <p className="inline-error chat-page-alert">{error}</p> : null}
      {activeThread?.contextTruncated && !error ? (
        <p className="inline-warning chat-page-alert">
          {activeThread.compactionMethod === "summarize" || activeThread.compactionMethod === "prune+summarize"
            ? "Conversation context was summarized to stay within the model limit. Earlier details are preserved in a condensed summary."
            : "Some conversation context was truncated to fit the model limit. The agent may be missing earlier details."}
        </p>
      ) : null}

      <section className="chat-layout-grid is-history-collapsed">
        <div
          className={
            isTraceResizing
              ? "chat-split-layout is-resizing"
              : isTracePanelOpen
                ? "chat-split-layout"
                : "chat-split-layout is-trace-collapsed"
          }
          ref={chatSplitRef}
          style={chatSplitStyle}
        >
          <section className="panel-surface chat-conversation-panel">
          <div className="chat-main-header stack-gap-sm">
            <div className="chat-main-title-row">
              <div className="stack-gap-2xs grow-block">
                {isRenamingTitle ? (
                  <label className="chat-title-edit">
                    <span className="detail-label">Session title</span>
                    <Input
                      className="chat-title-input"
                      onChange={(event) => setTitleDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          void handleSaveTitle();
                        }
                        if (event.key === "Escape") {
                          setIsRenamingTitle(false);
                          setTitleDraft(activeThread?.title || "");
                        }
                      }}
                      value={titleDraft}
                    />
                  </label>
                ) : (
                  <>
                    <h2 className="panel-title is-chat-title">{activeThread?.title || "New conversation"}</h2>
                    <p className="entity-meta">{currentAgent?.description || "Select an agent and continue the thread without losing context."}</p>
                  </>
                )}
              </div>
              <div className="chat-main-title-actions">
                {isRenamingTitle ? (
                  <>
                    <Button variant="ghost" size="sm" onClick={() => setIsRenamingTitle(false)} type="button">
                      Cancel
                    </Button>
                    <Button size="sm" onClick={() => void handleSaveTitle()} type="button">
                      Save
                    </Button>
                  </>
                ) : (
                  <>
                    <div className="chat-title-action-group">
                      <Button
                        className="chat-icon-button"
                        variant="ghost"
                        size="icon"
                        onClick={() => setIsRenamingTitle(true)}
                        type="button"
                        aria-label="Rename conversation"
                        title="Rename conversation"
                      >
                        <Pencil className="size-4" />
                      </Button>
                      <Button
                        className="chat-icon-button is-danger"
                        variant="ghost"
                        size="icon"
                        disabled={!activeThread}
                        onClick={() => void handleDeleteActiveThread()}
                        type="button"
                        aria-label="Delete conversation"
                        title="Delete conversation"
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    </div>
                    <div className="chat-title-action-group">
                      <Button
                        className="chat-icon-button"
                        variant="ghost"
                        size="icon"
                        onClick={() => setIsTracePanelOpen((open) => !open)}
                        type="button"
                        aria-label={isTracePanelOpen ? "Hide trace panel" : "Show trace panel"}
                        title={isTracePanelOpen ? "Hide trace panel" : "Show trace panel"}
                      >
                        {isTracePanelOpen ? <PanelRightClose className="size-4" /> : <PanelRight className="size-4" />}
                      </Button>
                    </div>
                  </>
                )}
              </div>
            </div>

            <div className="chat-toolbar-row">
              <div className="chat-agent-select">
                <Select
                  value={selectedAgent || null}
                  onValueChange={(value) => setSelectedAgent(value ?? "")}
                >
                  <SelectTrigger
                    className="chat-agent-select-trigger"
                    disabled={loading || agents.length === 0}
                    size="sm"
                    aria-label="Select agent"
                  >
                    <SelectValue placeholder={loading ? "Loading agents..." : "Select agent"} />
                  </SelectTrigger>
                  <SelectContent align="start" alignItemWithTrigger>
                    {agents.map((agent) => (
                      <SelectItem key={agent.name} value={agent.name}>
                        {agent.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="chat-meta-tags">
                <span className="soft-tag flex items-center gap-1.5">
                  {sending ? <span className="pulsing-glow-dot" /> : null}
                  {currentAgent?.provider.model || "No model"}
                </span>
                <span className="soft-tag">{currentAgent?.capabilities?.[0] || "Chat"}</span>
                <span className="soft-tag is-session-id" title={activeThread?.sessionId}>
                  {activeThread?.sessionId || "Contextualized"}
                </span>
              </div>
            </div>
          </div>

          <div
            className="message-stage is-console-stage"
            ref={messageStageRef}
            onScroll={handleMessageStageScroll}
          >
            {activeThread?.messagesHasMore ? (
              <button
                className="messages-older-load"
                disabled={activeThread.isLoadingOlderMessages}
                onClick={() => void loadOlderMessages()}
                type="button"
              >
                {activeThread.isLoadingOlderMessages ? "Loading earlier messages…" : "Load earlier messages"}
              </button>
            ) : null}
            {conversationMessages.length === 0 ? (
              <div className="chat-empty-state">
                <p className="section-kicker">{activeThread?.isPersisted ? "Loading" : "Start"}</p>
                <h3>{activeThread?.isPersisted ? "Loading conversation..." : "Start a persistent conversation."}</h3>
                <p>
                  {activeThread?.isPersisted
                    ? "Fetching stored messages and tool trace for this session."
                    : "Messages in this workspace now persist, keep context across replies, and auto-generate editable titles."}
                </p>
              </div>
            ) : (
              conversationMessages.map((message) => (
                <ChatMessageBubble
                  key={message.id}
                  message={message}
                  sending={sending}
                  messageTimestampFallback={activeThread?.updatedAt || 0}
                  editingMessageId={editingMessageId}
                  editingDraft={editingDraft}
                  onEditStart={(m) => {
                    setEditingMessageId(m.id);
                    setEditingDraft(m.content);
                  }}
                  onEditChange={setEditingDraft}
                  onEditCancel={() => {
                    setEditingMessageId(null);
                    setEditingDraft("");
                  }}
                  onEditSubmit={() => {
                    if (editingMessageId) {
                      const msg = activeThread?.messages.find((m) => m.id === editingMessageId);
                      if (msg) {
                        void handleEditResend(msg as Message, editingDraft);
                      }
                    }
                  }}
                />
              ))
            )}
          </div>

          <div className="composer-shell stack-gap-sm">
            {activePendingQuestion ? (
              <section className="pending-question-panel stack-gap-sm">
                <div className="stack-gap-2xs">
                  <p className="section-kicker">Input required</p>
                  <h3 className="pending-question-title">{activePendingQuestion.title}</h3>
                  <p className="helper-copy">Answer the questions below to let the agent resume this run.</p>
                </div>
                <div className="pending-question-list">
                  {activePendingQuestion.questions.map((question) => {
                    const draft = pendingDrafts[question.header] || { selectedOptions: [], freeform: "" };
                    return (
                      <article className="pending-question-card" key={question.header}>
                        <div className="stack-gap-2xs">
                          <strong>{question.header}</strong>
                          <p>{question.question}</p>
                          {question.message ? <p className="helper-copy">{question.message}</p> : null}
                          {question.multi_select && question.max_selections ? (
                            <p className="helper-copy">Choose up to {question.max_selections} options.</p>
                          ) : null}
                        </div>
                        {question.options.length ? (
                          <div className="pending-option-list">
                            {question.options.map((option) => {
                              const selected = draft.selectedOptions.includes(option.label);
                              return (
                                <label className={selected ? "pending-option-chip is-selected" : "pending-option-chip"} key={option.label}>
                                  <input
                                    checked={selected}
                                    name={`${activePendingQuestion.id}-${question.header}`}
                                    onChange={() => {
                                      setPendingDrafts((current) => {
                                        const existing = current[question.header] || { selectedOptions: [], freeform: "" };
                                        if (!question.multi_select) {
                                          return {
                                            ...current,
                                            [question.header]: { ...existing, selectedOptions: selected ? [] : [option.label] },
                                          };
                                        }
                                        const hasOption = existing.selectedOptions.includes(option.label);
                                        const nextOptions = hasOption
                                          ? existing.selectedOptions.filter((value) => value !== option.label)
                                          : [...existing.selectedOptions, option.label];
                                        if (question.max_selections && nextOptions.length > question.max_selections) {
                                          return current;
                                        }
                                        return {
                                          ...current,
                                          [question.header]: { ...existing, selectedOptions: nextOptions },
                                        };
                                      });
                                    }}
                                    type={question.multi_select ? "checkbox" : "radio"}
                                  />
                                  <span className="stack-gap-2xs pending-option-copy">
                                    <strong>{option.label}</strong>
                                    {option.description ? <span>{option.description}</span> : null}
                                  </span>
                                </label>
                              );
                            })}
                          </div>
                        ) : null}
                        {question.allow_freeform_input || question.options.length === 0 ? (
                          <Input
                            className="pending-response-input"
                            onChange={(event) => {
                              const nextValue = event.target.value;
                              setPendingDrafts((current) => ({
                                ...current,
                                [question.header]: {
                                  ...(current[question.header] || { selectedOptions: [], freeform: "" }),
                                  freeform: nextValue,
                                },
                              }));
                            }}
                            placeholder={question.options.length ? "Custom answer" : "Type your answer"}
                            value={draft.freeform}
                          />
                        ) : null}
                      </article>
                    );
                  })}
                </div>
                <div className="pending-question-actions">
                  <Button disabled={sending} onClick={() => void handleSubmitPendingQuestion()} type="button">
                    {sending ? "Continuing" : "Continue"}
                  </Button>
                </div>
              </section>
            ) : null}
            <div className="composer-inline composer-gemini">
              <input hidden multiple onChange={handleFileSelection} ref={fileInputRef} type="file" />
              <div className={`composer-gemini-input-area${composerMultiline ? " is-multiline" : ""}`}>
                <div className="composer-gemini-row">
                  {!composerMultiline && (
                    <Popover open={attachMenuOpen} onOpenChange={setAttachMenuOpen}>
                      <PopoverTrigger
                        className="composer-gemini-plus"
                        disabled={Boolean(activePendingQuestion) || sending || uploadingAttachments}
                        aria-label="Attach files"
                      >
                        <Plus className="size-5" />
                      </PopoverTrigger>
                      <PopoverContent align="start" side="top" className="w-64">
                        <div className="stack-gap-md" style={{ padding: "4px 0" }}>
                          <button
                            className="composer-gemini-menu-item"
                            disabled={Boolean(activePendingQuestion) || sending || uploadingAttachments}
                            onClick={() => {
                              fileInputRef.current?.click();
                              setAttachMenuOpen(false);
                            }}
                            type="button"
                          >
                            <Upload className="size-4" />
                            <span>Upload files</span>
                          </button>
                          <div className="composer-gemini-menu-divider" />
                          <label className="composer-gemini-menu-item composer-gemini-menu-check">
                            <input
                              checked={attachmentDeliveryMode === "parse"}
                              onChange={() => {
                                setAttachmentDeliveryModeTouched(true);
                                setAttachmentDeliveryMode((current) => (current === "parse" ? "workspace" : "parse"));
                              }}
                              type="checkbox"
                            />
                            <span>Parse uploads</span>
                            {attachmentDeliveryMode === "parse" ? <span className="composer-gemini-menu-check-badge">ON</span> : null}
                          </label>
                        </div>
                      </PopoverContent>
                    </Popover>
                  )}
                  <Textarea
                    className="chat-composer composer-gemini-textarea"
                    disabled={Boolean(activePendingQuestion) || sending}
                    onChange={(event) => setInput(event.target.value)}
                    onKeyDown={(event) => {
                      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                        event.preventDefault();
                        void handleSend();
                      }
                    }}
                    onPaste={(event) => {
                      void handleComposerPaste(event);
                    }}
                    placeholder={activePendingQuestion ? "Answer the pending questions above to continue..." : "Message the current agent..."}
                    ref={composerRef}
                    rows={1}
                    value={input}
                  />
                  {!composerMultiline && (
                    <Button
                      variant="default"
                      size="icon"
                      className="composer-gemini-send"
                      disabled={sending ? false : !currentAgent || (!input.trim() && attachmentDrafts.length === 0) || uploadingAttachments || Boolean(activePendingQuestion)}
                      onClick={sending ? () => void handleStopRun() : handleSend}
                      type="button"
                      aria-label={sending ? "Stop generating" : "Send message"}
                    >
                      {sending ? <Square className="size-4" /> : uploadingAttachments ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
                    </Button>
                  )}
                </div>
                {composerMultiline && (
                  <div className="composer-gemini-bottom-bar">
                    <Popover open={attachMenuOpen} onOpenChange={setAttachMenuOpen}>
                      <PopoverTrigger
                        className="composer-gemini-plus"
                        disabled={Boolean(activePendingQuestion) || sending || uploadingAttachments}
                        aria-label="Attach files"
                      >
                        <Plus className="size-5" />
                      </PopoverTrigger>
                      <PopoverContent align="start" side="top" className="w-64">
                        <div className="stack-gap-md" style={{ padding: "4px 0" }}>
                          <button
                            className="composer-gemini-menu-item"
                            disabled={Boolean(activePendingQuestion) || sending || uploadingAttachments}
                            onClick={() => {
                              fileInputRef.current?.click();
                              setAttachMenuOpen(false);
                            }}
                            type="button"
                          >
                            <Upload className="size-4" />
                            <span>Upload files</span>
                          </button>
                          <div className="composer-gemini-menu-divider" />
                          <label className="composer-gemini-menu-item composer-gemini-menu-check">
                            <input
                              checked={attachmentDeliveryMode === "parse"}
                              onChange={() => {
                                setAttachmentDeliveryModeTouched(true);
                                setAttachmentDeliveryMode((current) => (current === "parse" ? "workspace" : "parse"));
                              }}
                              type="checkbox"
                            />
                            <span>Parse uploads</span>
                            {attachmentDeliveryMode === "parse" ? <span className="composer-gemini-menu-check-badge">ON</span> : null}
                          </label>
                        </div>
                      </PopoverContent>
                    </Popover>
                    <Button
                      variant="default"
                      size="icon"
                      className="composer-gemini-send"
                      disabled={sending ? false : !currentAgent || (!input.trim() && attachmentDrafts.length === 0) || uploadingAttachments || Boolean(activePendingQuestion)}
                      onClick={sending ? () => void handleStopRun() : handleSend}
                      type="button"
                      aria-label={sending ? "Stop generating" : "Send message"}
                    >
                      {sending ? <Square className="size-4" /> : uploadingAttachments ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
                    </Button>
                  </div>
                )}
              </div>
            </div>
            {attachmentDrafts.length ? (
              <div className="composer-file-list" role="list">
                {attachmentDrafts.map((file) => (
                  <div className="composer-file-chip" key={file.id} role="listitem">
                    <div className="composer-file-body">
                      <div className="composer-file-topline">
                        <span className="composer-file-name">{file.name}</span>
                        <span className="composer-file-badge">{formatAttachmentBadge(file)}</span>
                      </div>
                      <span className="composer-file-meta">{formatAttachmentMeta(file)}</span>
                      {file.summary ? <span className="composer-file-summary">{file.summary}</span> : null}
                    </div>
                    <Button variant="ghost" className="composer-file-remove" onClick={() => handleRemoveFile(file.id)} type="button">
                      Remove
                    </Button>
                  </div>
                ))}
              </div>
            ) : null}
            <p
              className={
                attachmentDeliveryMode === "workspace" && !attachmentDeliveryModeTouched
                  ? "helper-copy composer-mode-hint is-active"
                  : attachmentDeliveryMode === "workspace"
                    ? "helper-copy composer-mode-hint is-inactive"
                    : "helper-copy composer-mode-hint is-active"
              }
            >
              {activePendingQuestion
                ? "The session is paused until you answer the pending questions."
                : uploadingAttachments
                  ? "Processing attachments before they are sent to the agent."
                  : attachmentDeliveryMode === "workspace" && !attachmentDeliveryModeTouched
                    ? "Parsing is off for most formats. Images and text files are still parsed into chat context by default."
                    : attachmentDeliveryMode === "workspace"
                      ? "Parsing is off. New uploads go to the agent workspace and are only announced with file paths."
                      : "Parsing is on. New uploads are parsed into chat context when that format is supported."}
            </p>
          </div>
          </section>

          {isTracePanelOpen ? (
            <div
              aria-controls="chat-trace-panel"
              aria-label="Resize agent trace panel"
              aria-orientation="vertical"
              aria-valuemax={MAX_TRACE_PANEL_WIDTH}
              aria-valuemin={MIN_TRACE_PANEL_WIDTH}
              aria-valuenow={tracePanelWidth}
              className="chat-trace-resizer"
              onKeyDown={handleTraceResizeKeyDown}
              onMouseDown={handleTraceResizeStart}
              role="separator"
              tabIndex={0}
              title="Drag to resize the trace panel"
            >
              <span className="chat-trace-resizer-grip" />
            </div>
          ) : null}

          {isTracePanelOpen ? (
            <aside className="panel-surface chat-trace-panel stack-gap-sm" id="chat-trace-panel">
              <div className="panel-title-row align-start-row">
                <div className="stack-gap-2xs grow-block">
                  <h2 className="panel-title is-trace-title">Agent trace</h2>
                  <div className="trace-filter-row">
                    <Badge variant="secondary" className="trace-pill">{displayedTraceEntries.length} events</Badge>
                    <Badge variant="outline" className="trace-pill">local only</Badge>
                  </div>
                </div>
              </div>

              <div className="trace-feed is-console-feed">
                {traceTurnGroups.length === 0 ? (
                  <p className="trace-empty-copy">
                    Trace events will appear here while the agent runs.
                  </p>
                ) : (
                  traceTurnGroups.map((turn) => {
                    const isCollapsed = collapsedTraceTurns.has(turn.turnIndex);
                    return (
                      <section
                        className={`trace-turn-group${isCollapsed ? " is-collapsed" : ""}`}
                        key={`turn-${turn.turnIndex}`}
                      >
                        <button
                          aria-expanded={!isCollapsed}
                          className="trace-turn-header"
                          onClick={() => toggleTraceTurn(turn.turnIndex)}
                          type="button"
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

                        {!isCollapsed ? (
                          <div className="trace-turn-steps">
                            <TraceNodeList nodes={turn.entries} sessionId={activeThread?.sessionId || ""} />
                          </div>
                        ) : null}
                      </section>
                    );
                  })
                )}
              </div>
            </aside>
          ) : null}
        </div>
      </section>
    </div>
  );
}
