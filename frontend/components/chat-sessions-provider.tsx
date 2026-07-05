"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { deleteChatSession, listChatSessions } from "@/lib/client-api";
import { buildChatHref, getChatSessionId } from "@/lib/chat-session-routing";
import {
  createThread,
  getTopQueuedThread,
  isReusableDraftThread,
  mergeSummaryIntoThread,
  resolveThreadId,
  threadFromSummary,
  type ChatThread,
} from "@/lib/chat-thread-model";
import type { ChatSessionSummary } from "@/lib/types";

type ChatSessionsContextValue = {
  threads: ChatThread[];
  setThreads: React.Dispatch<React.SetStateAction<ChatThread[]>>;
  loading: boolean;
  historyQuery: string;
  setHistoryQuery: (query: string) => void;
  activeThreadId: string;
  activeThread: ChatThread | null;
  chatHref: string;
  navigateToSession: (sessionId: string, replace?: boolean) => void;
  handleNewChat: (agentName?: string) => void;
  handleDeleteThread: (threadId: string, fallbackAgentName: string) => Promise<void>;
  updateThread: (threadId: string, updater: (thread: ChatThread) => ChatThread) => void;
  upsertThread: (nextThread: ChatThread) => void;
  applySessionSummary: (summary: ChatSessionSummary, fallbackThreadId?: string) => void;
};

const ChatSessionsContext = createContext<ChatSessionsContextValue | null>(null);

export function ChatSessionsProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const sessionFromUrl = getChatSessionId(searchParams);
  const isChatPage = pathname === "/";

  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [historyQuery, setHistoryQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const threadsRef = useRef<ChatThread[]>([]);

  useEffect(() => {
    threadsRef.current = threads;
  }, [threads]);

  useEffect(() => {
    let cancelled = false;

    async function loadSessions() {
      setLoading(true);
      try {
        const sessionResult = await listChatSessions();
        if (cancelled) {
          return;
        }
        const initialThreads = sessionResult.length
          ? sessionResult.map((session) => threadFromSummary(session))
          : [createThread("")];
        setThreads(initialThreads);
      } catch {
        if (!cancelled) {
          setThreads([createThread("")]);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadSessions();
    return () => {
      cancelled = true;
    };
  }, []);

  const activeThreadId = useMemo(
    () => resolveThreadId(threads, sessionFromUrl),
    [sessionFromUrl, threads],
  );

  const activeThread = useMemo(
    () => threads.find((thread) => thread.id === activeThreadId) ?? threads[0] ?? null,
    [activeThreadId, threads],
  );

  const chatHref = useMemo(() => buildChatHref(activeThreadId || null), [activeThreadId]);

  const navigateToSession = useCallback(
    (sessionId: string, replace = false) => {
      const href = buildChatHref(sessionId);
      if (replace) {
        router.replace(href);
        return;
      }
      router.push(href);
    },
    [router],
  );

  useEffect(() => {
    if (!isChatPage || loading) {
      return;
    }

    if (threads.length === 0) {
      const draft = createThread("");
      setThreads([draft]);
      navigateToSession(draft.id, true);
      return;
    }

    if (!sessionFromUrl) {
      navigateToSession(threads[0].id, true);
      return;
    }

    const exists = threads.some((thread) => thread.id === sessionFromUrl || thread.sessionId === sessionFromUrl);
    if (!exists) {
      navigateToSession(threads[0].id, true);
    }
  }, [isChatPage, loading, navigateToSession, sessionFromUrl, threads]);

  const updateThread = useCallback((threadId: string, updater: (thread: ChatThread) => ChatThread) => {
    setThreads((current) => current.map((thread) => (thread.id === threadId ? updater(thread) : thread)));
  }, []);

  const upsertThread = useCallback((nextThread: ChatThread) => {
    setThreads((current) => {
      const existingIndex = current.findIndex(
        (thread) => thread.id === nextThread.id || thread.sessionId === nextThread.sessionId,
      );
      if (existingIndex === -1) {
        return [nextThread, ...current];
      }
      return current.map((thread, index) => (index === existingIndex ? { ...thread, ...nextThread } : thread));
    });
  }, []);

  const applySessionSummary = useCallback(
    (summary: ChatSessionSummary, fallbackThreadId?: string) => {
      const previousId = fallbackThreadId || summary.id;
      setThreads((current) => {
        const threadId = fallbackThreadId || summary.id;
        const existing = current.find((thread) => thread.id === threadId || thread.sessionId === summary.id);
        const merged = mergeSummaryIntoThread(existing || createThread(summary.agent_name || ""), summary);
        if (!existing) {
          return [merged, ...current];
        }
        return current.map((thread) => (thread.id === existing.id ? merged : thread));
      });

      if (isChatPage && previousId !== summary.id && sessionFromUrl === previousId) {
        navigateToSession(summary.id, true);
      }
    },
    [isChatPage, navigateToSession, sessionFromUrl],
  );

  const handleNewChat = useCallback(
    (agentName = "") => {
      const resolvedAgentName = agentName || activeThread?.agentName || threadsRef.current[0]?.agentName || "";
      const topQueuedThread = getTopQueuedThread(threadsRef.current);
      if (topQueuedThread && isReusableDraftThread(topQueuedThread)) {
        navigateToSession(topQueuedThread.id, true);
        return;
      }

      const nextThread = createThread(resolvedAgentName);
      setThreads((current) => {
        const nextThreads = [nextThread, ...current];
        threadsRef.current = nextThreads;
        return nextThreads;
      });
      navigateToSession(nextThread.id, true);
    },
    [activeThread?.agentName, navigateToSession],
  );

  const handleDeleteThread = useCallback(
    async (threadId: string, fallbackAgentName: string) => {
      const target = threadsRef.current.find((thread) => thread.id === threadId);
      if (!target) {
        return;
      }

      if (target.isPersisted) {
        await deleteChatSession(target.sessionId);
      }

      const remaining = threadsRef.current.filter((thread) => thread.id !== threadId);
      const nextThreads = remaining.length ? remaining : [createThread(fallbackAgentName)];
      setThreads(nextThreads);

      if (activeThreadId === threadId) {
        navigateToSession(nextThreads[0]?.id || "", true);
      }
    },
    [activeThreadId, navigateToSession],
  );

  const value = useMemo<ChatSessionsContextValue>(
    () => ({
      threads,
      setThreads,
      loading,
      historyQuery,
      setHistoryQuery,
      activeThreadId,
      activeThread,
      chatHref,
      navigateToSession,
      handleNewChat,
      handleDeleteThread,
      updateThread,
      upsertThread,
      applySessionSummary,
    }),
    [
      activeThread,
      activeThreadId,
      applySessionSummary,
      chatHref,
      handleDeleteThread,
      handleNewChat,
      historyQuery,
      loading,
      navigateToSession,
      threads,
      updateThread,
      upsertThread,
    ],
  );

  return <ChatSessionsContext.Provider value={value}>{children}</ChatSessionsContext.Provider>;
}

export function useChatSessions() {
  const context = useContext(ChatSessionsContext);
  if (!context) {
    throw new Error("useChatSessions must be used within ChatSessionsProvider.");
  }
  return context;
}
