import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  Check,
  ListTree,
  MessageSquare,
  MessagesSquare,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { AgentSelectField } from "./AgentSelectField";
import { ImageAttachment } from "./ImageAttachment";
import { MarkdownContent } from "./MarkdownContent";
import { CopyAction, EditAction } from "./MessageActions";
import { ReasoningBlock } from "./ReasoningBlock";
import { TracePanel } from "./TracePanel";
import {
  buildDisplayMessages,
  formatMessageTimestamp,
  messagesBeforeUserOrdinal,
  type DisplayMessage,
  type PublishedFile,
} from "./transcript";

import { applyStreamEvent, emptyStream } from "./stream-state";

const TRACE_POLL_INTERVAL_MS = 1500;
const FELINES_MILO_AGENT = "felines-milo";

function agentDisplayName(name: string): string {
  return name === FELINES_MILO_AGENT ? "Milo" : name;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  }
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function DocumentChip({
  file,
  onDownload,
}: {
  file: PublishedFile;
  onDownload: (name: string) => void;
}) {
  return (
    <button
      className="chat-attachment-chip chat-attachment-chip-link"
      type="button"
      onClick={() => onDownload(file.name)}
    >
      <span className="chat-attachment-topline">
        <strong>{file.name}</strong>
        <span className="chat-attachment-badge">{file.kind.toUpperCase()}</span>
      </span>
      <span className="chat-attachment-meta">{formatFileSize(file.size)}</span>
      {file.summary ? (
        <span className="chat-attachment-summary">{file.summary}</span>
      ) : null}
      <span className="chat-attachment-action">Download</span>
    </button>
  );
}

function ThinkingIndicator() {
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

function SessionActionsMenu({
  title,
  pinned,
  armed,
  busy,
  onPin,
  onRename,
  onDelete,
}: {
  title: string;
  pinned: boolean;
  armed: boolean;
  busy: boolean;
  onPin: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!open) return;
    function updatePosition() {
      const anchor = triggerRef.current?.getBoundingClientRect();
      if (!anchor) return;
      const menuWidth = 176;
      const menuHeight = 120;
      setPosition({
        left: Math.max(
          8,
          Math.min(anchor.right - menuWidth, window.innerWidth - menuWidth - 8),
        ),
        top: Math.max(
          8,
          anchor.bottom + menuHeight + 8 > window.innerHeight
            ? anchor.top - menuHeight - 4
            : anchor.bottom + 4,
        ),
      });
    }
    function closeOutside(event: PointerEvent) {
      const target = event.target as Node;
      if (
        !triggerRef.current?.contains(target) &&
        !menuRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    }
    function closeEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    updatePosition();
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="session-action session-more"
        aria-label={`More actions for ${title}`}
        title="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => setOpen((current) => !current)}
      >
        <MoreHorizontal size={17} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="session-action-menu"
            role="menu"
            aria-label={`Actions for ${title}`}
            style={position}
            onKeyDown={(event) => {
              if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
              event.preventDefault();
              const buttons = [
                ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
                  "button:not(:disabled)",
                ),
              ];
              const index = buttons.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              buttons[
                (index +
                  (event.key === "ArrowDown" ? 1 : -1) +
                  buttons.length) %
                  buttons.length
              ]?.focus();
            }}
          >
            <button
              type="button"
              role="menuitem"
              disabled={busy}
              onClick={() => {
                onPin();
                setOpen(false);
              }}
            >
              {pinned ? <PinOff size={14} /> : <Pin size={14} />}
              {pinned ? "Unpin" : "Pin"}
            </button>
            <button
              type="button"
              role="menuitem"
              disabled={busy}
              onClick={() => {
                onRename();
                setOpen(false);
              }}
            >
              <Pencil size={14} />
              Rename
            </button>
            <button
              type="button"
              role="menuitem"
              className="is-danger"
              disabled={busy}
              onClick={() => {
                onDelete();
                if (armed) setOpen(false);
              }}
            >
              {armed ? <Check size={14} /> : <Trash2 size={14} />}
              {armed ? "Confirm delete" : "Delete"}
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

export function ChatWorkspace({
  status,
  felines,
  onEnterFelines,
}: {
  status: DesktopServiceStatus;
  felines: boolean;
  onEnterFelines: () => void;
}) {
  const [agents, setAgents] = useState<DesktopAgent[]>([]);
  const [sessions, setSessions] = useState<DesktopSessionSummary[]>([]);
  const [agentName, setAgentName] = useState("");
  const [session, setSession] = useState<DesktopSession | null>(null);
  const [draft, setDraft] = useState("");
  const [felinesNotice, setFelinesNotice] = useState("");
  const isFelinesCommand = draft.trim().toLowerCase() === "le chat";
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [streamOutput, setStreamOutput] = useState(emptyStream);
  const runningRef = useRef(false);
  const messageListRef = useRef<HTMLDivElement>(null);
  const followOutputRef = useRef(true);
  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (list && followOutputRef.current) list.scrollTop = list.scrollHeight;
  }, [streamOutput, session?.messages, busy]);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (runningRef.current) void window.covalentDesktop.cancelMessage().catch(() => {});
    };
  }, []);

  async function sendStreaming(value: Parameters<typeof window.covalentDesktop.streamMessage>[0]) {
    if (!mountedRef.current) throw new Error("Chat workspace closed");
    let output = emptyStream();
    let frame = 0;
    runningRef.current = true;
    setStreamOutput(output);
    try {
      return await window.covalentDesktop.streamMessage(value, (event) => {
        if (!mountedRef.current) return;
        output = applyStreamEvent(output, event);
        if (!frame) frame = requestAnimationFrame(() => {
          frame = 0;
          if (mountedRef.current) setStreamOutput(output);
        });
      });
    } catch (cause) {
      runningRef.current = false;
      // Failed edits/runs may already have persisted changes. Never restore a
      // stale pre-run transcript or automatically retry tool side effects.
      if (value.session_id && mountedRef.current) {
        try { setSession(await window.covalentDesktop.getSession(value.session_id)); }
        catch { /* Keep the last visible state if the service is unavailable. */ }
      }
      throw cause;
    } finally {
      runningRef.current = false;
      cancelAnimationFrame(frame);
      if (mountedRef.current) setStreamOutput(output);
    }
  }

  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [conversationsVisible, setConversationsVisible] = useState(true);
  const [traceVisible, setTraceVisible] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editingDraft, setEditingDraft] = useState("");
  const [renamingTitle, setRenamingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [renamingItemKey, setRenamingItemKey] = useState<string | null>(null);
  const [itemRenameDraft, setItemRenameDraft] = useState("");
  const [deleteArmKey, setDeleteArmKey] = useState<string | null>(null);
  const deleteArmTimer = useRef<number | null>(null);

  useEffect(() => {
    if (status.phase !== "ready") return;
    let active = true;
    Promise.all([
      window.covalentDesktop.listAgents(felines),
      window.covalentDesktop.listSessions(),
    ])
      .then(([agentResult, sessionResult]) => {
        if (!active) return;
        const chatAgents = agentResult.items.filter(
          (agent) => agent.enabled && agent.capabilities.includes("chat"),
        );
        setAgents(chatAgents);
        setSessions(sessionResult.items);
        setAgentName((previous) =>
          chatAgents.some((agent) => agent.name === previous)
            ? previous
            : chatAgents[0]?.name || "",
        );
        setSession((current) =>
          current && chatAgents.some((agent) => agent.name === current.agent_name)
            ? current
            : null,
        );
        setLoaded(true);
      })
      .catch((cause) => active && setError(String(cause)));
    return () => {
      active = false;
    };
  }, [status.phase, felines]);

  async function openSession(id: string) {
    if (busy) return;
    try {
      setError("");
      const value = await window.covalentDesktop.getSession(id);
      followOutputRef.current = true;
      setSession(value);
      setAnswers({});
      setRenamingTitle(false);
      setTitleDraft("");
      setAgentName(value.agent_name);
    } catch (cause) {
      setError(String(cause));
    }
  }

  async function send() {
    const message = draft.trim();
    // A standalone local command: never create a conversation or invoke an agent.
    if (message.toLowerCase() === "le chat") {
      onEnterFelines();
      setDraft("");
      setFelinesNotice(
        "Meow. Felines mode is on — use the cat in the top bar to leave.",
      );
      return;
    }
    if (
      !message ||
      status.phase !== "ready" ||
      Boolean(session?.input_request) ||
      !agentName ||
      !agents.some((agent) => agent.name === agentName) ||
      busy
    )
      return;
    followOutputRef.current = true;
    setBusy(true);
    setStreamOutput(emptyStream());
    setError("");
    setDraft("");
    const previous = session;
    const title = session?.title || message.slice(0, 80);
    try {
      // The conversation is created up front so trace events of this first turn
      // can be polled while the agent is still running.
      const sessionId = previous
        ? previous.id
        : (await window.covalentDesktop.createConversation(agentName, title))
            .session_id;
      if (!mountedRef.current) return;
      setSession({
        id: sessionId,
        agent_name: agentName,
        title,
        created_at: previous?.created_at || "",
        pinned: false,
        messages: [
          ...(previous?.messages || []),
          { role: "user", content: message },
        ],
        activity: previous?.activity || [],
      });
      const result = await sendStreaming({
        agent_name: agentName,
        message,
        include_felines: felines,
        session_id: sessionId,
      });
      const [full, recent] = await Promise.all([
        window.covalentDesktop.getSession(result.session_id),
        window.covalentDesktop.listSessions(),
      ]);
      setSession(full);
      setSessions(recent.items);
    } catch (cause) {
      setError(String(cause));
      setDraft(message);
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    if (!session?.input_request || busy) return;
    if (
      session.input_request.questions.some(
        (question) => !answers[question.header]?.trim(),
      )
    ) {
      setError("Answer each question before continuing.");
      return;
    }
    followOutputRef.current = true;
    setBusy(true);
    setStreamOutput(emptyStream());
    setError("");
    try {
      const result = await sendStreaming({
        agent_name: session.agent_name,
        include_felines: felines,
        session_id: session.id,
        message: "",
        resume_answers: answers,
      });
      setSession(await window.covalentDesktop.getSession(result.session_id));
      setAnswers({});
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  const userMessages = useMemo(
    () => (session?.messages ?? []).filter((item) => item.role === "user"),
    [session?.messages],
  );

  const fetchActivityRaw = useCallback(
    async (activityId: string) => {
      if (!session?.id) throw new Error("No conversation selected");
      return window.covalentDesktop.getSessionActivity(session.id, activityId);
    },
    [session?.id],
  );

  // Poll only persisted trace metadata. Text comes from the live stream; a
  // late poll must never overwrite the authoritative completion snapshot.
  const polledSessionId = session?.id ?? null;
  useEffect(() => {
    if (!busy || !polledSessionId) return;
    let active = true;
    let pending = false;
    const timer = window.setInterval(() => {
      if (pending || !runningRef.current) return;
      pending = true;
      window.covalentDesktop
        .getSession(polledSessionId)
        .then((polled) => {
          if (!active || !runningRef.current) return;
          setSession((current) => {
            if (!current || current.id !== polledSessionId) return current;
            return { ...current, activity: polled.activity, turn_meta: polled.turn_meta };
          });
        })
        .catch(() => {
          /* Transient poll failures recover on the next tick. */
        })
        .finally(() => { pending = false; });
    }, TRACE_POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [busy, polledSessionId]);

  // Charts render only for Agents that declare the capability, like Enterprise.
  const enableCharts = useMemo(
    () =>
      agents
        .find((agent) => agent.name === agentName)
        ?.capabilities.includes("chart") ?? false,
    [agents, agentName],
  );

  const displayMessages = useMemo(
    () =>
      buildDisplayMessages(session?.messages ?? [], session?.turn_meta ?? []),
    [session?.messages, session?.turn_meta],
  );

  const saveDownload = useCallback(
    (name: string) => {
      if (!session?.id) return;
      void window.covalentDesktop
        .saveDownload(session.id, name)
        .catch((cause) => setError(String(cause)));
    },
    [session?.id],
  );

  const openExternal = useCallback((url: string) => {
    void window.covalentDesktop
      .openExternal(url)
      .catch((cause) => setError(String(cause)));
  }, []);

  function startEdit(message: DisplayMessage) {
    setEditingKey(message.key);
    setEditingDraft(message.content);
    setError("");
  }

  function cancelEdit() {
    setEditingKey(null);
    setEditingDraft("");
  }

  // Editing rewrites history from the edited turn onward: the sidecar drops the
  // stored transcript and trace at that point, then runs the edited text.
  async function submitEdit() {
    const draft = editingDraft.trim();
    const previous = session;
    const target = displayMessages.find((item) => item.key === editingKey);
    if (!draft || !previous || !target?.userOrdinal || busy) return;
    followOutputRef.current = true;
    setBusy(true);
    setStreamOutput(emptyStream());
    setError("");
    setEditingKey(null);
    setSession({
      ...previous,
      messages: [
        ...messagesBeforeUserOrdinal(previous.messages, target.userOrdinal),
        { role: "user", content: draft },
      ],
      activity: (previous.activity ?? []).filter(
        (item) => item.turn < target.turn,
      ),
      turn_meta: (previous.turn_meta ?? []).filter(
        (entry) => entry.turn < target.turn,
      ),
      input_request: null,
      suggestions: [],
    });
    try {
      const result = await sendStreaming({
        agent_name: previous.agent_name,
        message: draft,
        include_felines: felines,
        session_id: previous.id,
        edit_user_index: target.userOrdinal,
      });
      const [full, recent] = await Promise.all([
        window.covalentDesktop.getSession(result.session_id),
        window.covalentDesktop.listSessions(),
      ]);
      setSession(full);
      setSessions(recent.items);
    } catch (cause) {
      setError(String(cause));
      setEditingKey(target.key);
      setEditingDraft(draft);
    } finally {
      setBusy(false);
    }
  }

  function startRenameTitle() {
    if (!session) return;
    setTitleDraft(session.title);
    setRenamingTitle(true);
    setError("");
  }

  function cancelRenameTitle() {
    setRenamingTitle(false);
    setTitleDraft("");
  }

  async function saveTitle() {
    const title = titleDraft.trim();
    if (!title || !session) return;
    try {
      const updated = await window.covalentDesktop.renameSession(
        session.id,
        title,
      );
      setSession((current) =>
        current ? { ...current, title: updated.title } : current,
      );
      setSessions((await window.covalentDesktop.listSessions()).items);
      setRenamingTitle(false);
      setTitleDraft("");
    } catch (cause) {
      setError(String(cause));
    }
  }

  const visibleSessions = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const modeSessions = felines
      ? sessions
      : sessions.filter((item) => item.agent_name !== FELINES_MILO_AGENT);
    if (!query) return modeSessions;
    return modeSessions.filter(
      (item) =>
        item.title.toLowerCase().includes(query) ||
        item.agent_name.toLowerCase().includes(query),
    );
  }, [sessions, searchQuery, felines]);

  function startItemRename(item: DesktopSessionSummary) {
    disarmSessionDelete();
    setRenamingItemKey(item.id);
    setItemRenameDraft(item.title);
    setError("");
  }

  function cancelItemRename() {
    setRenamingItemKey(null);
    setItemRenameDraft("");
  }

  async function saveItemRename(item: DesktopSessionSummary) {
    const title = itemRenameDraft.trim();
    if (!title || busy) return;
    try {
      await window.covalentDesktop.renameSession(item.id, title);
      setSessions((current) =>
        current.map((entry) =>
          entry.id === item.id ? { ...entry, title } : entry,
        ),
      );
      setSession((current) =>
        current?.id === item.id ? { ...current, title } : current,
      );
      cancelItemRename();
    } catch (cause) {
      setError(String(cause));
    }
  }

  function armSessionDelete(item: DesktopSessionSummary) {
    cancelItemRename();
    setDeleteArmKey(item.id);
    if (deleteArmTimer.current !== null) {
      window.clearTimeout(deleteArmTimer.current);
    }
    deleteArmTimer.current = window.setTimeout(() => {
      setDeleteArmKey((key) => (key === item.id ? null : key));
      deleteArmTimer.current = null;
    }, 3500);
  }

  function disarmSessionDelete() {
    if (deleteArmTimer.current !== null) {
      window.clearTimeout(deleteArmTimer.current);
      deleteArmTimer.current = null;
    }
    setDeleteArmKey(null);
  }

  async function removeSession(item: DesktopSessionSummary) {
    if (busy) return;
    try {
      await window.covalentDesktop.deleteSession(item.id);
      disarmSessionDelete();
      setSessions((current) => current.filter((entry) => entry.id !== item.id));
      setSession((current) => (current?.id === item.id ? null : current));
    } catch (cause) {
      setError(String(cause));
    }
  }

  async function toggleSessionPin(item: DesktopSessionSummary) {
    try {
      await window.covalentDesktop.pinSession(item.id, !item.pinned);
      setSessions((await window.covalentDesktop.listSessions()).items);
    } catch (cause) {
      setError(String(cause));
    }
  }

  const liveReasoning = busy ? streamOutput.reasoning : "";

  function startNewConversation() {
    if (busy) return;
    setSession(null);
    setAnswers({});
    setError("");
    setRenamingTitle(false);
    setTitleDraft("");
  }

  return (
    <div
      className={`chat-layout ${conversationsVisible ? "" : "conversations-hidden"} ${traceVisible ? "" : "trace-hidden"}`}
      data-loaded={loaded}
    >
      <section
        className="surface conversation-list"
        aria-hidden={!conversationsVisible}
        inert={!conversationsVisible}
      >
        <div className="surface-heading">
          <span>Recent conversations</span>
          <button
            className="icon-button session-create"
            type="button"
            aria-label="New conversation"
            title="New conversation"
            disabled={busy}
            onClick={startNewConversation}
          >
            <Plus size={16} />
          </button>
        </div>
        <div className="session-search">
          <Search size={14} />
          <input
            className="session-search-input"
            aria-label="Search conversations"
            placeholder="Search conversations"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
        </div>
        <div className="session-list">
          {sessions.length === 0 ? (
            <div className="list-empty">
              <MessageSquare size={22} />
              <strong>No conversations yet</strong>
              <p>
                Your conversation will be saved locally after the first message.
              </p>
            </div>
          ) : visibleSessions.length === 0 ? (
            <div className="list-empty">
              <Search size={22} />
              <strong>No matching conversations.</strong>
            </div>
          ) : (
            visibleSessions.map((item) => {
              const isRenaming = renamingItemKey === item.id;
              const armed = deleteArmKey === item.id;
              return (
                <div
                  className={`session-item ${session?.id === item.id ? "selected" : ""}${item.pinned ? " pinned" : ""}${armed ? " is-armed" : ""}`}
                  key={item.id}
                >
                  {isRenaming ? (
                    <input
                      className="session-rename-input"
                      aria-label="Conversation title"
                      value={itemRenameDraft}
                      autoFocus
                      onChange={(event) =>
                        setItemRenameDraft(event.target.value)
                      }
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          void saveItemRename(item);
                        } else if (event.key === "Escape") {
                          event.preventDefault();
                          cancelItemRename();
                        }
                      }}
                    />
                  ) : (
                    <button
                      type="button"
                      className="session-open"
                      disabled={busy}
                      onClick={() => openSession(item.id)}
                    >
                      <MessageSquare size={15} />
                      <span className="session-item-body">
                        <strong className="session-title">
                          {item.pinned && <Pin size={11} />}
                          <span className="session-title-text">
                            {item.title}
                          </span>
                        </strong>
                        <small>{agentDisplayName(item.agent_name)}</small>
                      </span>
                    </button>
                  )}
                  <div
                    aria-label="Conversation actions"
                    className={`session-actions${armed ? " is-armed" : ""}`}
                    role="group"
                  >
                    {isRenaming ? (
                      <>
                        <button
                          type="button"
                          className="session-action is-confirm"
                          aria-label="Save title"
                          title="Save title"
                          disabled={!itemRenameDraft.trim() || busy}
                          onClick={() => void saveItemRename(item)}
                        >
                          <Check size={13} />
                        </button>
                        <button
                          type="button"
                          className="session-action"
                          aria-label="Cancel rename"
                          title="Cancel"
                          onClick={cancelItemRename}
                        >
                          <X size={13} />
                        </button>
                      </>
                    ) : (
                      <SessionActionsMenu
                        title={item.title}
                        pinned={item.pinned}
                        armed={armed}
                        busy={busy}
                        onPin={() => void toggleSessionPin(item)}
                        onRename={() => startItemRename(item)}
                        onDelete={() =>
                          armed
                            ? void removeSession(item)
                            : armSessionDelete(item)
                        }
                      />
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </section>
      <section className="surface conversation-panel">
        <div className="surface-heading">
          <div className="chat-heading-group">
            <button
              className={`icon-button chat-rail-toggle ${conversationsVisible ? "is-active" : ""}`}
              type="button"
              aria-label={
                conversationsVisible
                  ? "Hide conversations"
                  : "Show conversations"
              }
              title={
                conversationsVisible
                  ? "Hide conversations"
                  : "Show conversations"
              }
              aria-expanded={conversationsVisible}
              onClick={() => setConversationsVisible((visible) => !visible)}
            >
              <MessagesSquare size={16} />
            </button>
            {renamingTitle ? (
              <div className="chat-title-editor">
                <input
                  className="chat-title-input"
                  aria-label="Conversation title"
                  value={titleDraft}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void saveTitle();
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      cancelRenameTitle();
                    }
                  }}
                />
                <button
                  className="secondary-button"
                  type="button"
                  onClick={cancelRenameTitle}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="button"
                  disabled={!titleDraft.trim()}
                  onClick={() => void saveTitle()}
                >
                  Save
                </button>
              </div>
            ) : (
              <>
                <span
                  className="chat-heading-title"
                  title={session?.title || "Conversation"}
                >
                  {session?.title || "Conversation"}
                </span>
                {session ? (
                  <button
                    className="icon-button"
                    type="button"
                    aria-label="Rename conversation"
                    title="Rename conversation"
                    onClick={startRenameTitle}
                  >
                    <Pencil size={14} />
                  </button>
                ) : null}
              </>
            )}
          </div>
          <div className="chat-heading-group chat-heading-actions">
            <button
              className="icon-button"
              type="button"
              aria-label="New conversation"
              title="New conversation"
              disabled={busy}
            onClick={startNewConversation}
            >
              <Plus size={16} />
            </button>
            <AgentSelectField
              className="chat-agent-picker"
              hideLabel
              label="Select agent"
              value={agentName}
              onChange={(value) => {
                setAgentName(value);
                setSession(null);
              }}
              disabled={!agents.length || busy}
              options={agents.map((agent) => ({
                value: agent.name,
                label: agentDisplayName(agent.name),
              }))}
              placeholder="No agents"
            />
            <button
              className="icon-button"
              type="button"
              aria-label={
                traceVisible ? "Hide execution trace" : "Show execution trace"
              }
              title={
                traceVisible ? "Hide execution trace" : "Show execution trace"
              }
              aria-expanded={traceVisible}
              onClick={() => setTraceVisible((visible) => !visible)}
            >
              <ListTree size={16} />
            </button>
          </div>
        </div>
        <div className="message-list" ref={messageListRef} onScroll={(event) => {
          const list = event.currentTarget;
          followOutputRef.current = list.scrollHeight - list.scrollTop - list.clientHeight < 64;
        }}>
          {displayMessages.length ? (
            displayMessages.map((message) => {
              const isUser = message.role === "user";
              const isEditing = editingKey === message.key;
              return (
                <div
                  className={`chat-message ${isUser ? "user" : "assistant"}`}
                  key={message.key}
                >
                  <div className="chat-message-stack">
                    <span className="message-role">
                      {isUser ? "You" : agentDisplayName(agentName)}
                    </span>
                    <div
                      className={`chat-bubble${isEditing ? " is-editing" : ""}`}
                    >
                      {!isUser && message.reasoning ? (
                        <ReasoningBlock
                          reasoning={message.reasoning}
                          active={false}
                        />
                      ) : null}
                      {message.files.length > 0 ? (
                        <div className="chat-attachment-list">
                          {message.files.map((file) =>
                            file.kind === "image" ? (
                              <ImageAttachment
                                file={file}
                                key={file.id}
                                sessionId={session?.id ?? ""}
                                onDownload={saveDownload}
                              />
                            ) : (
                              <DocumentChip
                                file={file}
                                key={file.id}
                                onDownload={saveDownload}
                              />
                            ),
                          )}
                        </div>
                      ) : null}
                      {isEditing ? (
                        <div className="chat-bubble-edit">
                          <textarea
                            className="chat-bubble-edit-input"
                            aria-label="Edit message"
                            rows={3}
                            value={editingDraft}
                            onChange={(event) =>
                              setEditingDraft(event.target.value)
                            }
                          />
                          <div className="chat-bubble-edit-actions">
                            <button type="button" onClick={cancelEdit}>
                              Cancel
                            </button>
                            <button
                              type="button"
                              disabled={!editingDraft.trim() || busy}
                              onClick={() => void submitEdit()}
                            >
                              Save &amp; resend
                            </button>
                          </div>
                        </div>
                      ) : message.content ? (
                        <MarkdownContent
                          content={message.content}
                          enableCharts={enableCharts}
                          onDownload={saveDownload}
                          onOpenExternal={openExternal}
                          tone={isUser ? "outbound" : "inbound"}
                        />
                      ) : null}
                    </div>
                    <div
                      aria-label="Message actions"
                      className="chat-message-actions"
                      role="group"
                    >
                      {message.time !== null ? (
                        <time
                          className="chat-message-time"
                          dateTime={new Date(message.time).toISOString()}
                        >
                          {formatMessageTimestamp(message.time)}
                        </time>
                      ) : null}
                      <CopyAction content={message.content} />
                      {isUser && !busy ? (
                        <EditAction onEdit={() => startEdit(message)} />
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="chat-welcome">
              <span className="welcome-icon">
                <MessageSquare size={24} />
              </span>
              <h3>
                {agents.length
                  ? "Start a new conversation"
                  : "Create or activate a chat agent first"}
              </h3>
              <p>
                {agents.length
                  ? "Select an agent and send a message. Conversations are saved locally."
                  : "Open Agent settings to configure an active agent with chat capability."}
              </p>
            </div>
          )}
          {busy && liveReasoning ? (
            <div className="chat-message assistant">
              <span className="message-role">{agentDisplayName(agentName)}</span>
              <div>
                <ReasoningBlock reasoning={liveReasoning} active />
              </div>
            </div>
          ) : null}
          {busy && streamOutput.text ? (
            <div className="chat-message assistant">
              <span className="message-role">{agentDisplayName(agentName)}</span>
              <MarkdownContent content={streamOutput.text} enableCharts={false}
                onDownload={saveDownload} onOpenExternal={openExternal} tone="inbound" />
            </div>
          ) : null}
          {busy && !liveReasoning && !streamOutput.text ? <ThinkingIndicator /> : null}
        </div>
        {!!session?.suggestions?.length && (
          <div className="suggested-questions">
            {session.suggestions.map((question) => (
              <button
                key={question}
                type="button"
                className="secondary-button"
                onClick={() => setDraft(question)}
              >
                {question}
              </button>
            ))}
          </div>
        )}
        {session?.input_request && (
          <div className="input-request">
            <h3>{session.input_request.title}</h3>
            {session.input_request.questions.map((question) =>
              question.options.length ? (
                <AgentSelectField
                  key={question.header}
                  label={question.question}
                  value={answers[question.header] ?? ""}
                  onChange={(value) =>
                    setAnswers((current) => ({
                      ...current,
                      [question.header]: value,
                    }))
                  }
                  options={question.options.map((option) => ({
                    value: option.label,
                    label: option.label,
                  }))}
                  placeholder="Choose an answer..."
                />
              ) : (
                <label key={question.header}>
                  {question.question}
                  <input
                    value={answers[question.header] ?? ""}
                    onChange={(event) =>
                      setAnswers((current) => ({
                        ...current,
                        [question.header]: event.target.value,
                      }))
                    }
                    placeholder="Your answer"
                  />
                </label>
              ),
            )}
            <button
              type="button"
              className="primary-button"
              disabled={busy}
              onClick={() => void resume()}
            >
              Continue Agent
            </button>
          </div>
        )}
        {error && (
          <p className="chat-error" role="alert">
            {error}
          </p>
        )}
        {felinesNotice && (
          <p className="felines-notice" role="status">
            {felinesNotice}
          </p>
        )}
        <div className="composer">
          <textarea
            aria-label="Message"
            placeholder={
              agents.length
                ? "Message your agent…"
                : "Create or activate a chat agent first"
            }
            value={draft}
            disabled={busy || Boolean(session?.input_request)}
            onChange={(event) => {
              setDraft(event.target.value);
              setFelinesNotice("");
            }}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={
              !draft.trim() ||
              (!isFelinesCommand && (
                !agents.some((agent) => agent.name === agentName) ||
                status.phase !== "ready" ||
                Boolean(session?.input_request) ||
                busy
              ))
            }
            aria-label="Send message"
          >
            <Send size={16} />
          </button>
        </div>
      </section>
      <section
        className="surface trace-panel"
        aria-hidden={!traceVisible}
        inert={!traceVisible}
      >
        <TracePanel
          activity={session?.activity ?? []}
          userMessages={userMessages}
          onFetchRaw={fetchActivityRaw}
        />
      </section>
    </div>
  );
}
