import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
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

const TRACE_POLL_INTERVAL_MS = 1500;

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
        <span className="chat-attachment-badge">
          {file.kind.toUpperCase()}
        </span>
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

export function ChatWorkspace({ status }: { status: DesktopServiceStatus }) {
  const [agents, setAgents] = useState<DesktopAgent[]>([]);
  const [sessions, setSessions] = useState<DesktopSessionSummary[]>([]);
  const [agentName, setAgentName] = useState("");
  const [session, setSession] = useState<DesktopSession | null>(null);
  const [draft, setDraft] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
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
      window.covalentDesktop.listAgents(),
      window.covalentDesktop.listSessions(),
    ])
      .then(([agentResult, sessionResult]) => {
        if (!active) return;
        const chatAgents = agentResult.items.filter(
          (agent) => agent.enabled && agent.capabilities.includes("chat"),
        );
        setAgents(chatAgents);
        setSessions(sessionResult.items);
        setAgentName((previous) => previous || chatAgents[0]?.name || "");
        setLoaded(true);
      })
      .catch((cause) => active && setError(String(cause)));
    return () => {
      active = false;
    };
  }, [status.phase]);

  async function openSession(id: string) {
    try {
      setError("");
      const value = await window.covalentDesktop.getSession(id);
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
    if (
      !message ||
      !agentName ||
      !agents.some((agent) => agent.name === agentName) ||
      busy
    )
      return;
    setBusy(true);
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
      const result = await window.covalentDesktop.sendMessage({
        agent_name: agentName,
        message,
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
      setSession(previous);
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
    setBusy(true);
    setError("");
    try {
      const result = await window.covalentDesktop.sendMessage({
        agent_name: session.agent_name,
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

  // Trace entries are written by the service as the run streams, so polling is
  // enough to follow a running turn without a streaming transport.
  const polledSessionId = session?.id ?? null;
  useEffect(() => {
    if (!busy || !polledSessionId) return;
    let active = true;
    const timer = window.setInterval(() => {
      window.covalentDesktop
        .getSession(polledSessionId)
        .then((polled) => {
          if (!active) return;
          setSession((current) => {
            if (!current || current.id !== polledSessionId) return current;
            // The runtime persists the transcript only once the run ends, so a
            // mid-run poll would otherwise drop the optimistic user message.
            return {
              ...polled,
              messages:
                polled.messages.length >= current.messages.length
                  ? polled.messages
                  : current.messages,
            };
          });
        })
        .catch(() => {
          /* Transient poll failures recover on the next tick. */
        });
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
    () => buildDisplayMessages(session?.messages ?? [], session?.turn_meta ?? []),
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
    setBusy(true);
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
      const result = await window.covalentDesktop.sendMessage({
        agent_name: previous.agent_name,
        message: draft,
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
      setSession(previous);
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
    if (!query) return sessions;
    return sessions.filter(
      (item) =>
        item.title.toLowerCase().includes(query) ||
        item.agent_name.toLowerCase().includes(query),
    );
  }, [sessions, searchQuery]);

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

  const liveReasoning = busy ? (session?.live_reasoning ?? "") : "";

  return (
    <div
      className={`chat-layout ${conversationsVisible ? "" : "conversations-hidden"} ${traceVisible ? "" : "trace-hidden"}`}
      data-loaded={loaded}
    >
      {conversationsVisible && (
        <section className="surface conversation-list">
          <div className="surface-heading">Recent conversations</div>
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
                  Your conversation will be saved locally after the first
                  message.
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
                    className={`session-item ${session?.id === item.id ? "selected" : ""}${armed ? " is-armed" : ""}`}
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
                          <small>{item.agent_name}</small>
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
                        <>
                          <button
                            type="button"
                            className="session-action"
                            aria-label={
                              item.pinned
                                ? "Unpin conversation"
                                : "Pin conversation"
                            }
                            title={
                              item.pinned
                                ? "Unpin conversation"
                                : "Pin conversation"
                            }
                            disabled={busy}
                            onClick={() => void toggleSessionPin(item)}
                          >
                            {item.pinned ? (
                              <PinOff size={13} />
                            ) : (
                              <Pin size={13} />
                            )}
                          </button>
                          <button
                            type="button"
                            className="session-action"
                            aria-label="Rename conversation"
                            title="Rename conversation"
                            disabled={busy}
                            onClick={() => startItemRename(item)}
                          >
                            <Pencil size={13} />
                          </button>
                          {armed ? (
                            <button
                              type="button"
                              className="session-action is-danger"
                              aria-label="Confirm delete"
                              title="Confirm delete"
                              disabled={busy}
                              onClick={() => void removeSession(item)}
                            >
                              <Check size={13} />
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="session-action is-danger"
                              aria-label="Delete conversation"
                              title="Delete conversation"
                              disabled={busy}
                              onClick={() => armSessionDelete(item)}
                            >
                              <Trash2 size={13} />
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </section>
      )}
      <section className="surface conversation-panel">
        <div className="surface-heading">
          <div className="chat-heading-group">
            <button
              className="icon-button"
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
              {conversationsVisible ? (
                <PanelLeftClose size={16} />
              ) : (
                <PanelLeftOpen size={16} />
              )}
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
              onClick={() => {
                setSession(null);
                setAnswers({});
                setError("");
                setRenamingTitle(false);
                setTitleDraft("");
              }}
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
                label: agent.name,
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
              {traceVisible ? (
                <PanelRightClose size={16} />
              ) : (
                <PanelRightOpen size={16} />
              )}
            </button>
          </div>
        </div>
        <div className="message-list">
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
                      {isUser ? "You" : agentName}
                    </span>
                    <div className={`chat-bubble${isEditing ? " is-editing" : ""}`}>
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
              <span className="message-role">{agentName}</span>
              <div>
                <ReasoningBlock reasoning={liveReasoning} active />
              </div>
            </div>
          ) : null}
          {busy && !liveReasoning ? <ThinkingIndicator /> : null}
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
        <div className="composer">
          <textarea
            aria-label="Message"
            placeholder={
              agents.length
                ? "Message your agent…"
                : "Create or activate a chat agent first"
            }
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            disabled={
              !agents.some((agent) => agent.name === agentName) ||
              status.phase !== "ready" ||
              Boolean(session?.input_request) ||
              busy
            }
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={
              !draft.trim() ||
              !agents.some((agent) => agent.name === agentName) ||
              status.phase !== "ready" ||
              Boolean(session?.input_request) ||
              busy
            }
            aria-label="Send message"
          >
            <Send size={16} />
          </button>
        </div>
      </section>
      {traceVisible && (
        <section className="surface trace-panel">
          <TracePanel
            activity={session?.activity ?? []}
            userMessages={userMessages}
            onFetchRaw={fetchActivityRaw}
          />
        </section>
      )}
    </div>
  );
}
