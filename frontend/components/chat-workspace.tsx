"use client";

import {
  isValidElement,
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
  PanelLeft,
  PanelLeftClose,
  PanelRight,
  PanelRightClose,
  Pencil,
  Plus,
  Trash2,
  Upload,
} from "lucide-react";

import {
  deleteChatSession,
  getAgents,
  getChatSession,
  getHealth,
  listChatSessions,
  renameChatSession,
  sortAgentsForPicker,
  streamAgent,
  uploadChatAttachments,
} from "@/lib/client-api";
import type {
  AgentDetail,
  AgentInputPart,
  AttachmentDeliveryMode,
  AttachmentUploadItem,
  ChatSession,
  ChatSessionSummary,
  HealthResponse,
  PendingQuestionRequest,
} from "@/lib/types";

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: ComposerAttachment[];
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
};

type PendingQuestionDraft = {
  selectedOptions: string[];
  freeform: string;
};

type ChatThread = {
  id: string;
  title: string;
  titleSource: "auto" | "manual";
  sessionId: string;
  agentName: string;
  messages: Message[];
  activity: ActivityItem[];
  createdAt: number;
  updatedAt: number;
  previewText: string;
  isLoaded: boolean;
  isPersisted: boolean;
  pendingQuestion: PendingQuestionRequest | null;
  contextTruncated: boolean;
  compactionMethod: string | null;
};

type HistorySection = {
  label: string;
  items: ChatThread[];
};

const TRACE_PANEL_STORAGE_KEY = "agent-framework.chat-trace-width";
const TRACE_PANEL_VISIBLE_STORAGE_KEY = "agent-framework.chat-trace-visible";
const HISTORY_PANEL_VISIBLE_STORAGE_KEY = "agent-framework.chat-history-visible";
const DEFAULT_TRACE_PANEL_WIDTH = 360;
const MIN_TRACE_PANEL_WIDTH = 280;
const MAX_TRACE_PANEL_WIDTH = 640;
const MIN_CONVERSATION_PANEL_WIDTH = 560;
const CODE_BLOCK_COPY_RESET_MS = 1600;

type ChatCodeBlockTone = "inbound" | "outbound";

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

function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function buildAttachmentId(file: Pick<File, "name" | "size" | "lastModified">, deliveryMode?: AttachmentDeliveryMode): string {
  return `${file.name}-${file.size}-${file.lastModified}-${deliveryMode || "default"}`;
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

function ChatBubbleCopy({ content, tone }: { content: string; tone: "inbound" | "outbound" }) {
  const copyResetRef = useRef<number | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied">("idle");
  const isOutbound = tone === "outbound";

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
      className="chat-bubble-copy"
      onClick={handleCopy}
      type="button"
      aria-label="Copy message"
      style={{
        position: "absolute",
        top: 6,
        right: 6,
        zIndex: 2,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: 28,
        height: 28,
        borderRadius: 6,
        border: isOutbound ? "1px solid rgba(255, 255, 255, 0.32)" : "1px solid rgba(16, 16, 16, 0.12)",
        background: isOutbound ? "rgba(255, 255, 255, 0.22)" : "#ffffff",
        boxShadow: isOutbound ? "0 6px 14px rgba(0, 0, 0, 0.16)" : "0 6px 14px rgba(16, 16, 16, 0.1)",
        color: isOutbound ? "var(--fg-inverse)" : "var(--fg-primary)",
        cursor: "pointer",
        fontSize: 13,
        lineHeight: 1,
        padding: 0,
      } satisfies CSSProperties}
    >
      {copyState === "copied" ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
    background: isOutbound ? "rgba(255, 255, 255, 0.1)" : "#f6f6f6",
    boxShadow: isOutbound ? "inset 0 0 0 1px rgba(255, 255, 255, 0.12)" : "inset 0 0 0 1px rgba(16, 16, 16, 0.06)",
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
    border: isOutbound ? "1px solid rgba(255, 255, 255, 0.18)" : "1px solid rgba(16, 16, 16, 0.08)",
    background: isOutbound ? "rgba(255, 255, 255, 0.14)" : "#ffffff",
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

function ChatMessageBubble({ message, sending }: { message: Message; sending: boolean }) {
  const tone = message.role === "user" ? "outbound" : "inbound";

  return (
    <article className={message.role === "user" ? "chat-message-row outbound" : "chat-message-row inbound"}>
      <div className={message.role === "user" ? "chat-bubble outbound" : "chat-bubble inbound"}>
        <ChatBubbleCopy content={message.content || ""} tone={tone} />
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
            {message.content || (sending && message.role === "assistant" ? "Thinking..." : "")}
          </ReactMarkdown>
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

function createThread(agentName = ""): ChatThread {
  const sessionId = uid("session");
  return {
    id: sessionId,
    title: "New conversation",
    titleSource: "auto",
    sessionId,
    agentName,
    messages: [],
    activity: [],
    createdAt: Date.now(),
    updatedAt: Date.now(),
    previewText: "",
    isLoaded: true,
    isPersisted: false,
    pendingQuestion: null,
    contextTruncated: false,
    compactionMethod: null,
  };
}

function pickAvailableAgentName(agents: AgentDetail[], ...candidates: Array<string | null | undefined>): string {
  for (const candidate of candidates) {
    const normalized = candidate?.trim();
    if (!normalized) {
      continue;
    }
    if (agents.some((agent) => agent.name === normalized)) {
      return normalized;
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

function threadFromSummary(summary: ChatSessionSummary): ChatThread {
  return {
    id: summary.id,
    title: summary.title,
    titleSource: summary.title_source,
    sessionId: summary.id,
    agentName: summary.agent_name || "",
    messages: [],
    activity: [],
    createdAt: toTimestamp(summary.created_at),
    updatedAt: toTimestamp(summary.updated_at),
    previewText: summary.preview_text,
    isLoaded: false,
    isPersisted: true,
    pendingQuestion: null,
    contextTruncated: false,
    compactionMethod: null,
  };
}

function threadFromSession(session: ChatSession): ChatThread {
  return {
    id: session.id,
    title: session.title,
    titleSource: session.title_source,
    sessionId: session.id,
    agentName: session.agent_name || "",
    messages: session.messages.map((message) => ({
      id: message.id,
      role: message.role,
      content: message.content,
      attachments: message.attachments.map((attachment) => normalizeAttachment(attachment)),
    })),
    activity: session.activity.map((item) => ({ id: item.id, title: item.title, payload: item.payload })),
    createdAt: toTimestamp(session.created_at),
    updatedAt: toTimestamp(session.updated_at),
    previewText: session.preview_text,
    isLoaded: true,
    isPersisted: true,
    pendingQuestion: getPendingQuestionFromActivity(session.activity.map((item) => ({ id: item.id, title: item.title, payload: item.payload }))),
    contextTruncated: false,
    compactionMethod: null,
  };
}

function mergeSummaryIntoThread(thread: ChatThread, summary: ChatSessionSummary): ChatThread {
  return {
    ...thread,
    id: summary.id,
    title: summary.title,
    titleSource: summary.title_source,
    sessionId: summary.id,
    agentName: summary.agent_name || thread.agentName,
    createdAt: toTimestamp(summary.created_at),
    updatedAt: toTimestamp(summary.updated_at),
    previewText: summary.preview_text || thread.previewText,
    isPersisted: true,
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
    hour12: false,
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
  const text = typeof payload === "string" ? payload : `${JSON.stringify(payload, null, 2)}\n`;
  if (text.length <= 4_000) {
    return text;
  }
  return `${text.slice(0, 4_000).trimEnd()}\n... truncated ...\n`;
}

function truncateTraceSummaryText(value: string, maxChars = 240): string {
  const normalized = value.trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(maxChars - 3, 1)).trimEnd()}...`;
}

const DELEGATE_EVENT_PREFIX = "delegate_";

function isDelegateEventTitle(value: string): boolean {
  return value.startsWith(DELEGATE_EVENT_PREFIX);
}

function getBaseEventTitle(value: string): string {
  return isDelegateEventTitle(value) ? value.slice(DELEGATE_EVENT_PREFIX.length) : value;
}

function getTraceSourceBadges(isDelegate: boolean, payload: Record<string, unknown>): string[] {
  if (!isDelegate) {
    return [];
  }

  const agentName = typeof payload.agent_name === "string" ? payload.agent_name.trim() : "";
  const delegatedBy = typeof payload.delegated_by === "string" ? payload.delegated_by.trim() : "";
  const parentIteration = Number(payload.parent_iteration) || 0;

  return [
    agentName ? `delegate ${agentName}` : "delegate",
    delegatedBy ? `via ${delegatedBy}` : "",
    parentIteration > 0 ? `parent ${parentIteration}` : "",
  ].filter(Boolean);
}

function withTraceSourcePrefix(isDelegate: boolean, payload: Record<string, unknown>, summary: string): string {
  if (!isDelegate) {
    return summary;
  }

  const agentName = typeof payload.agent_name === "string" ? payload.agent_name.trim() : "";
  if (!agentName) {
    return `Delegate: ${summary}`;
  }

  const delegatedBy = typeof payload.delegated_by === "string" ? payload.delegated_by.trim() : "";
  const sourceLabel = delegatedBy ? `${agentName} via ${delegatedBy}` : agentName;
  return `Delegate ${sourceLabel}: ${summary}`;
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
    const originalChars = Number(payload.original_char_count) || 0;
    const requestChars = Number(payload.request_char_count) || 0;
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
    const requestChars = Number(payload.request_char_count) || 0;
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

  return null;
}

function getTraceBadges(item: ActivityItem): string[] {
  const payload = asTracePayloadRecord(item.payload);
  if (!payload) {
    return [];
  }
  const baseTitle = getBaseEventTitle(item.title);
  const isDelegate = isDelegateEventTitle(item.title);

  if (baseTitle === "context_window") {
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      `${payload.iteration || "?"} iter`,
      `${payload.request_message_count || 0} msgs`,
      `${formatCompactNumber(Number(payload.request_char_count) || 0)} chars`,
      Number(payload.tool_message_compaction_count) > 0 ? `${payload.tool_message_compaction_count} tool trims` : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "model_call") {
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      `${payload.iteration || "?"} iter`,
      typeof payload.phase === "string" ? payload.phase : "react",
      formatDurationLabel(payload.elapsed_ms) || "",
      payload.status === "error" ? `HTTP ${payload.status_code || 502}` : `${payload.tool_call_count || 0} tools`,
      payload.compacted ? "compacted" : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "tool_calls") {
    const toolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls.length : 0;
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      toolCalls ? `${toolCalls} call${toolCalls === 1 ? "" : "s"}` : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "tool_results") {
    const results = Array.isArray(payload.results) ? payload.results.length : 0;
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      results ? `${results} result${results === 1 ? "" : "s"}` : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "thought") {
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      `${payload.iteration || "?"} iter`,
      typeof payload.stage === "string" ? payload.stage : "react",
      typeof payload.kind === "string" ? payload.kind.replaceAll("_", " ") : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "iteration") {
    return [
      ...getTraceSourceBadges(isDelegate, payload),
      typeof payload.iteration === "number" ? `${payload.iteration} iter` : "",
    ].filter(Boolean) as string[];
  }

  if (baseTitle === "assistant") {
    return [...getTraceSourceBadges(isDelegate, payload), `${payload.iteration || "?"} iter`, "stream"].filter(Boolean) as string[];
  }

  if (baseTitle === "final") {
    return [...getTraceSourceBadges(isDelegate, payload), "complete"].filter(Boolean) as string[];
  }

  if (baseTitle === "input_required") {
    const questions = Array.isArray(payload.questions) ? payload.questions.length : 0;
    return [...getTraceSourceBadges(isDelegate, payload), questions ? `${questions} questions` : "awaiting input"].filter(Boolean) as string[];
  }

  if (baseTitle === "error") {
    const statusCode = Number(payload.status_code) || 0;
    return [...getTraceSourceBadges(isDelegate, payload), statusCode ? `HTTP ${statusCode}` : ""].filter(Boolean) as string[];
  }

  return [];
}

function historyLabel(timestamp: number): string {
  const now = new Date();
  const target = new Date(timestamp);
  const midnightNow = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const midnightTarget = new Date(target.getFullYear(), target.getMonth(), target.getDate()).getTime();
  const diffDays = Math.floor((midnightNow - midnightTarget) / 86400000);

  if (diffDays <= 0) {
    return "Today";
  }
  if (diffDays === 1) {
    return "Yesterday";
  }
  if (diffDays < 7) {
    return "Last 7 days";
  }
  return "Earlier";
}

function isTraceStreamEvent(event: string): boolean {
  const baseEvent = getBaseEventTitle(event);
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

function isReusableDraftThread(thread: ChatThread): boolean {
  return !thread.isPersisted && thread.messages.length === 0 && thread.titleSource !== "manual";
}

function getTopQueuedThread(items: ChatThread[]): ChatThread | null {
  return items.reduce<ChatThread | null>((latest, thread) => {
    if (!latest || thread.updatedAt > latest.updatedAt) {
      return thread;
    }
    return latest;
  }, null);
}

export function ChatWorkspace() {
  const [, setHealth] = useState<HealthResponse | null>(null);
  const [agents, setAgents] = useState<AgentDetail[]>([]);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [input, setInput] = useState("");
  const [draftAttachments, setDraftAttachments] = useState<ComposerAttachment[]>([]);
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState("");
  const [historyQuery, setHistoryQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const [attachmentDeliveryMode, setAttachmentDeliveryMode] = useState<AttachmentDeliveryMode>("workspace");
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [composerMultiline, setComposerMultiline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isRenamingTitle, setIsRenamingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [pendingDrafts, setPendingDrafts] = useState<Record<string, PendingQuestionDraft>>({});
  const [tracePanelWidth, setTracePanelWidth] = useState(DEFAULT_TRACE_PANEL_WIDTH);
  const [isTracePanelOpen, setIsTracePanelOpen] = useState(true);
  const [isHistoryPanelOpen, setIsHistoryPanelOpen] = useState(true);
  const [isTraceResizing, setIsTraceResizing] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const chatSplitRef = useRef<HTMLDivElement | null>(null);
  const threadsRef = useRef<ChatThread[]>([]);
  const skipLayoutPersistRef = useRef(true);

  useEffect(() => {
    threadsRef.current = threads;
  }, [threads]);

  useEffect(() => {
    const traceStored = window.localStorage.getItem(TRACE_PANEL_VISIBLE_STORAGE_KEY);
    if (traceStored === "0") {
      setIsTracePanelOpen(false);
    }

    const historyStored = window.localStorage.getItem(HISTORY_PANEL_VISIBLE_STORAGE_KEY);
    if (historyStored === "0") {
      setIsHistoryPanelOpen(false);
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
    window.localStorage.setItem(HISTORY_PANEL_VISIBLE_STORAGE_KEY, isHistoryPanelOpen ? "1" : "0");
  }, [isHistoryPanelOpen]);

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
        const [healthResult, agentResult, sessionResult] = await Promise.all([getHealth(), getAgents(), listChatSessions()]);
        if (cancelled) {
          return;
        }
        const sortedAgents = sortAgentsForPicker(agentResult);
        const initialThreads = sessionResult.length
          ? sessionResult.map((session) => threadFromSummary(session))
          : [createThread(sortedAgents[0]?.name || "")];
        setHealth(healthResult);
        setAgents(sortedAgents);
        setSelectedAgent((current) => pickAvailableAgentName(sortedAgents, current, initialThreads[0]?.agentName, sortedAgents[0]?.name));
        setThreads(initialThreads);
        setActiveThreadId((current) => current || initialThreads[0]?.id || "");
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
    if (!loading && threads.length === 0) {
      const nextThread = createThread(selectedAgent);
      setThreads([nextThread]);
      setActiveThreadId(nextThread.id);
      return;
    }

    if (!activeThreadId || !threads.some((thread) => thread.id === activeThreadId)) {
      setActiveThreadId(threads[0]?.id || "");
    }
  }, [activeThreadId, loading, selectedAgent, threads]);

  const activeThread = useMemo(
    () => threads.find((thread) => thread.id === activeThreadId) ?? threads[0] ?? null,
    [activeThreadId, threads],
  );

  useEffect(() => {
    const nextSelectedAgent = pickAvailableAgentName(agents, selectedAgent, activeThread?.agentName, threads[0]?.agentName);
    if (nextSelectedAgent !== selectedAgent) {
      setSelectedAgent(nextSelectedAgent);
    }
  }, [activeThread?.agentName, agents, selectedAgent, threads]);

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
        const session = await getChatSession(activeThread.sessionId);
        if (cancelled) {
          return;
        }
        const hydratedThread = threadFromSession(session);
        setThreads((current) => current.map((thread) => (thread.id === activeThread.id ? hydratedThread : thread)));
        setSelectedAgent((current) => pickAvailableAgentName(agents, current, hydratedThread.agentName));
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
  }, [activeThread, agents]);

  useEffect(() => {
    setDraftAttachments([]);
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

  const visibleThreads = useMemo(() => {
    const query = historyQuery.trim().toLowerCase();
    const sorted = [...threads].sort((left, right) => right.updatedAt - left.updatedAt);
    if (!query) {
      return sorted;
    }
    return sorted.filter((thread) => `${thread.title} ${thread.agentName} ${thread.sessionId} ${thread.previewText}`.toLowerCase().includes(query));
  }, [historyQuery, threads]);

  const historySections = useMemo<HistorySection[]>(() => {
    const grouped = new Map<string, ChatThread[]>();
    for (const thread of visibleThreads) {
      const label = historyLabel(thread.updatedAt);
      grouped.set(label, [...(grouped.get(label) || []), thread]);
    }
    return ["Today", "Yesterday", "Last 7 days", "Earlier"]
      .map((label) => ({ label, items: grouped.get(label) || [] }))
      .filter((section) => section.items.length > 0);
  }, [visibleThreads]);

  const traceEntries = useMemo(() => {
    const items = activeThread?.activity || [];
    return items.map((item, index) => {
      const timestamp = getTimestampFromId(item.id, activeThread?.updatedAt || Date.now());
      const baseTitle = getBaseEventTitle(item.title);
      const payload = asTracePayloadRecord(item.payload);
      const isDelegate = isDelegateEventTitle(item.title);
      return {
        ...item,
        label: isDelegate
          ? "Delegate"
          : baseTitle === "error"
            ? "Error"
            : baseTitle === "tool_results"
              ? "Result"
              : baseTitle === "tool_calls"
                ? "Tool"
                : baseTitle === "iteration"
                  ? "Iteration"
                  : baseTitle === "thought"
                    ? "Thought"
                : baseTitle === "model_call"
                  ? "Model"
                  : baseTitle === "context_window"
                    ? "Context"
                    : baseTitle === "assistant"
                      ? "Stream"
                      : baseTitle === "final"
                        ? "Final"
                        : baseTitle === "input_required" || baseTitle === "input_resolved"
                      ? "Input"
                      : "Event",
        displayTime: formatTime(timestamp),
      };
    });
  }, [activeThread]);

  const conversationMessages = activeThread?.messages || [];
  const displayedTraceEntries = traceEntries;
  const activePendingQuestion = activeThread?.pendingQuestion || null;
  const chatSplitStyle = {
    "--chat-trace-width": `${tracePanelWidth}px`,
  } as CSSProperties;

  useEffect(() => {
    setPendingDrafts(buildInitialPendingDrafts(activePendingQuestion));
  }, [activePendingQuestion]);

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

  function updateThread(threadId: string, updater: (thread: ChatThread) => ChatThread) {
    setThreads((current) => current.map((thread) => (thread.id === threadId ? updater(thread) : thread)));
  }

  function upsertThread(nextThread: ChatThread) {
    setThreads((current) => {
      const existingIndex = current.findIndex((thread) => thread.id === nextThread.id || thread.sessionId === nextThread.sessionId);
      if (existingIndex === -1) {
        return [nextThread, ...current];
      }
      return current.map((thread, index) => (index === existingIndex ? { ...thread, ...nextThread } : thread));
    });
  }

  function applySessionSummary(summary: ChatSessionSummary, fallbackThreadId?: string) {
    setThreads((current) => {
      const threadId = fallbackThreadId || summary.id;
      const existing = current.find((thread) => thread.id === threadId || thread.sessionId === summary.id);
      const merged = mergeSummaryIntoThread(existing || createThread(summary.agent_name || selectedAgent), summary);
      if (!existing) {
        return [merged, ...current];
      }
      return current.map((thread) => (thread.id === existing.id ? merged : thread));
    });
  }

  function handleNewChat() {
    const topQueuedThread = getTopQueuedThread(threadsRef.current);
    if (topQueuedThread && isReusableDraftThread(topQueuedThread)) {
      setActiveThreadId(topQueuedThread.id);
      setInput("");
      setDraftAttachments([]);
      setError(null);
      return;
    }
    const nextThread = createThread(selectedAgent);
    setThreads((current) => {
      const nextThreads = [nextThread, ...current];
      threadsRef.current = nextThreads;
      return nextThreads;
    });
    setActiveThreadId(nextThread.id);
    setInput("");
    setDraftAttachments([]);
    setError(null);
  }

  async function queueComposerFiles(incomingFiles: File[]) {
    if (incomingFiles.length === 0 || !activeThread) {
      return;
    }

    const knownIds = new Set(draftAttachments.map((file) => file.id));
    const uniqueFiles = incomingFiles.filter((file) => !knownIds.has(buildAttachmentId(file, attachmentDeliveryMode)));
    if (uniqueFiles.length === 0) {
      return;
    }

    setError(null);
    setUploadingAttachments(true);
    try {
      const uploaded = (await uploadChatAttachments(activeThread.sessionId, uniqueFiles, attachmentDeliveryMode)).files.map((file) =>
        toUploadedComposerAttachment(file),
      );
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

  async function handleDeleteThread(threadId: string) {
    const target = threads.find((thread) => thread.id === threadId);
    if (!target) {
      return;
    }

    setError(null);
    try {
      if (target.isPersisted) {
        await deleteChatSession(target.sessionId);
      }
      const remaining = threads.filter((thread) => thread.id !== threadId);
      const nextThreads = remaining.length ? remaining : [createThread(selectedAgent)];
      setThreads(nextThreads);
      if (activeThreadId === threadId) {
        setActiveThreadId(nextThreads[0]?.id || "");
      }
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : "Failed to delete conversation.");
    }
  }

  async function runThreadRequest(params: {
    thread: ChatThread;
    requestInput: string | AgentInputPart[];
    userContent: string;
    attachments?: ComposerAttachment[];
    metadata?: Record<string, unknown>;
    clearPendingQuestion?: boolean;
  }) {
    const attachments = params.attachments || [];
    const threadId = params.thread.id;
    const userMessageId = uid("user");
    const assistantId = uid("assistant");
    const userMessage: Message = { id: userMessageId, role: "user", content: params.userContent, attachments };

    setError(null);
    setSending(true);

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

    let streamTerminatedCleanly = false;

    try {
      await streamAgent(
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
        ({ event, payload }) => {
          if (event === "assistant") {
            const text = (payload as { text?: string })?.text || "";
            updateThread(threadId, (thread) => ({
              ...thread,
              updatedAt: Date.now(),
              messages: thread.messages.map((message) =>
                message.id === assistantId ? { ...message, content: `${message.content}${text}` } : message,
              ),
            }));
            return;
          }

          if (event === "final") {
            streamTerminatedCleanly = true;
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

          if (event === "session") {
            const sessionSummary = payload as ChatSessionSummary;
            applySessionSummary(sessionSummary, threadId);
            if (activeThreadId === threadId && sessionSummary.id !== threadId) {
              setActiveThreadId(sessionSummary.id);
            }
            return;
          }

          if (event === "input_required") {
            streamTerminatedCleanly = true;
            const pendingQuestion = normalizePendingQuestionRequest(payload);
            updateThread(threadId, (thread) => ({
              ...thread,
              updatedAt: Date.now(),
              pendingQuestion,
              activity: [...thread.activity, { id: uid(event), title: event, payload }],
              messages: thread.messages.map((message) =>
                message.id === assistantId ? { ...message, content: message.content || "Waiting for your answer…" } : message,
              ),
            }));
            return;
          }

          if (event === "error") {
            streamTerminatedCleanly = true;
            const detail =
              (payload as { detail?: string })?.detail ||
              (payload as { message?: string })?.message ||
              "Agent run failed.";
            setError(detail);
            updateThread(threadId, (thread) => ({
              ...thread,
              updatedAt: Date.now(),
              activity: [...thread.activity, { id: uid(event), title: event, payload }],
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
              activity: [...thread.activity, { id: uid(event), title: event, payload }],
            }));
            return;
          }

          if (isTraceStreamEvent(event)) {
            const publishedDownloads = getBaseEventTitle(event) === "tool_results" ? extractPublishedDownloadsFromPayload(payload) : [];
            updateThread(threadId, (thread) => ({
              ...thread,
              updatedAt: Date.now(),
              activity: [...thread.activity, { id: uid(event), title: event, payload }],
              messages: publishedDownloads.length
                ? thread.messages.map((message) =>
                    message.id === assistantId
                      ? { ...message, attachments: mergeAttachments(message.attachments || [], publishedDownloads) }
                      : message,
                  )
                : thread.messages,
            }));
            return;
          }
        },
      );

      if (!streamTerminatedCleanly) {
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
    } catch (sendError) {
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
      setSending(false);
    }
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

      <section className={isHistoryPanelOpen ? "chat-layout-grid" : "chat-layout-grid is-history-collapsed"}>
        {isHistoryPanelOpen ? (
          <aside className="panel-surface chat-history-panel">
            <div className="chat-sidebar-header">
              <h2 className="chat-sidebar-title">Sessions</h2>
              <div className="chat-sidebar-header-actions">
                <Button className="chat-new-button" onClick={handleNewChat} type="button" variant="outline">
                  <Plus className="size-3.5" />
                  New chat
                </Button>
              </div>
            </div>

            <label className="search-field chat-search-field">
              <Input onChange={(event) => setHistoryQuery(event.target.value)} placeholder="Search" value={historyQuery} />
            </label>

            <div className="history-stack">
              {historySections.map((section) => (
                <section className="history-section" key={section.label}>
                  <p className="history-section-label">{section.label}</p>
                  <div className="history-section-list">
                    {section.items.map((thread) => (
                      <button
                        className={thread.id === activeThread?.id ? "history-card is-active" : "history-card"}
                        key={thread.id}
                        onClick={() => {
                          setActiveThreadId(thread.id);
                          setSelectedAgent((current) => pickAvailableAgentName(agents, thread.agentName, current));
                        }}
                        type="button"
                      >
                        <strong>{thread.title}</strong>
                      </button>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </aside>
        ) : null}

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
                      {!isHistoryPanelOpen ? (
                        <Button variant="outline" onClick={handleNewChat} type="button">
                          <Plus className="size-3.5" />
                          New chat
                        </Button>
                      ) : null}
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
                        onClick={() => void handleDeleteThread(activeThread?.id || "")}
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
                        onClick={() => setIsHistoryPanelOpen((open) => !open)}
                        type="button"
                        aria-label={isHistoryPanelOpen ? "Hide sessions panel" : "Show sessions panel"}
                        title={isHistoryPanelOpen ? "Hide sessions panel" : "Show sessions panel"}
                      >
                        {isHistoryPanelOpen ? <PanelLeftClose className="size-4" /> : <PanelLeft className="size-4" />}
                      </Button>
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
                <span className="soft-tag">{currentAgent?.provider.model || "No model"}</span>
                <span className="soft-tag">{currentAgent?.capabilities?.[0] || "Chat"}</span>
                <span className="soft-tag is-session-id" title={activeThread?.sessionId}>
                  {activeThread?.sessionId || "Contextualized"}
                </span>
              </div>
            </div>
          </div>

          <div className="message-stage is-console-stage">
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
                <ChatMessageBubble key={message.id} message={message} sending={sending} />
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
                              onChange={() => setAttachmentDeliveryMode((current) => (current === "parse" ? "workspace" : "parse"))}
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
                      disabled={!currentAgent || (!input.trim() && attachmentDrafts.length === 0) || sending || uploadingAttachments || Boolean(activePendingQuestion)}
                      onClick={handleSend}
                      type="button"
                      aria-label="Send message"
                    >
                      {uploadingAttachments ? <Loader2 className="size-4 animate-spin" /> : sending ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
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
                              onChange={() => setAttachmentDeliveryMode((current) => (current === "parse" ? "workspace" : "parse"))}
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
                      disabled={!currentAgent || (!input.trim() && attachmentDrafts.length === 0) || sending || uploadingAttachments || Boolean(activePendingQuestion)}
                      onClick={handleSend}
                      type="button"
                      aria-label="Send message"
                    >
                      {uploadingAttachments ? <Loader2 className="size-4 animate-spin" /> : sending ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
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
                attachmentDeliveryMode === "workspace"
                  ? "helper-copy composer-mode-hint is-inactive"
                  : "helper-copy composer-mode-hint is-active"
              }
            >
              {activePendingQuestion
                ? "The session is paused until you answer the pending questions."
                : uploadingAttachments
                  ? "Processing attachments before they are sent to the agent."
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
                {displayedTraceEntries.map((item) => (
                  <article className="trace-entry compact-trace-entry" key={item.id}>
                    <div className="trace-entry-head">
                      <Badge variant="secondary" className="trace-pill">{item.label}</Badge>
                      <span className="trace-time">{item.displayTime}</span>
                    </div>
                    <strong>{formatActivityTitle(item.title)}</strong>
                    {getTraceSummary(item) ? <p className="trace-summary-copy">{getTraceSummary(item)}</p> : null}
                    {getTraceBadges(item).length ? (
                      <div className="trace-badge-row">
                        {getTraceBadges(item).map((badge) => (
                          <span className="trace-inline-badge" key={`${item.id}-${badge}`}>
                            {badge}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    <pre className="trace-note">{formatTracePayload(item.payload)}</pre>
                  </article>
                ))}
              </div>
            </aside>
          ) : null}
        </div>
      </section>
    </div>
  );
}
