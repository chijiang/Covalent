import type { ChatSessionSummary, PendingQuestionRequest } from "@/lib/types";

export type ChatThreadMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: unknown[];
};

export type ChatThreadActivity = {
  id: string;
  title: string;
  payload: unknown;
};

export type ChatThread = {
  id: string;
  title: string;
  titleSource: "auto" | "manual";
  sessionId: string;
  agentName: string;
  messages: ChatThreadMessage[];
  activity: ChatThreadActivity[];
  createdAt: number;
  updatedAt: number;
  previewText: string;
  isLoaded: boolean;
  isPersisted: boolean;
  pendingQuestion: PendingQuestionRequest | null;
  contextTruncated: boolean;
  compactionMethod: string | null;
};

export type ChatHistorySection = {
  label: string;
  items: ChatThread[];
};

export function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function toTimestamp(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : Date.now();
}

export function createThread(agentName = ""): ChatThread {
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

export function threadFromSummary(summary: ChatSessionSummary): ChatThread {
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

export function mergeSummaryIntoThread(thread: ChatThread, summary: ChatSessionSummary): ChatThread {
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

export function historyLabel(timestamp: number): string {
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

export function buildHistorySections(threads: ChatThread[]): ChatHistorySection[] {
  const grouped = new Map<string, ChatThread[]>();
  for (const thread of threads) {
    const label = historyLabel(thread.updatedAt);
    grouped.set(label, [...(grouped.get(label) || []), thread]);
  }
  return ["Today", "Yesterday", "Last 7 days", "Earlier"]
    .map((label) => ({ label, items: grouped.get(label) || [] }))
    .filter((section) => section.items.length > 0);
}

export function filterThreadsByQuery(threads: ChatThread[], query: string): ChatThread[] {
  const normalized = query.trim().toLowerCase();
  const sorted = [...threads].sort((left, right) => right.updatedAt - left.updatedAt);
  if (!normalized) {
    return sorted;
  }
  return sorted.filter((thread) =>
    `${thread.title} ${thread.agentName} ${thread.sessionId} ${thread.previewText}`.toLowerCase().includes(normalized),
  );
}

export function isReusableDraftThread(thread: ChatThread): boolean {
  return !thread.isPersisted && thread.messages.length === 0 && thread.titleSource !== "manual";
}

export function getTopQueuedThread(items: ChatThread[]): ChatThread | null {
  return items.reduce<ChatThread | null>((latest, thread) => {
    if (!latest || thread.updatedAt > latest.updatedAt) {
      return thread;
    }
    return latest;
  }, null);
}

export function resolveThreadId(threads: ChatThread[], sessionId: string | null): string {
  if (!sessionId) {
    return threads[0]?.id || "";
  }
  const match = threads.find((thread) => thread.id === sessionId || thread.sessionId === sessionId);
  return match?.id || sessionId;
}
