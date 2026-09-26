import { useEffect, useState } from "react";
import {
  Activity,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Send,
} from "lucide-react";
import { ChartBlock } from "./ChartBlock";

function contentText(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content);
}

function MessageContent({ content }: { content: unknown }) {
  const text = contentText(content);
  const chunks = text.split(/(```echarts\s*\n[\s\S]*?```)/g);
  return (
    <>
      {chunks.map((chunk, index) =>
        chunk.startsWith("```echarts") ? (
          <ChartBlock
            key={index}
            source={chunk
              .replace(/^```echarts\s*\n/, "")
              .replace(/```$/, "")
              .trim()}
          />
        ) : (
          <span key={index} className="chat-text-chunk">
            {chunk}
          </span>
        ),
      )}
    </>
  );
}

function publishedDownloads(
  messages: DesktopMessage[],
): { name: string; summary: string }[] {
  const found = new Map<string, { name: string; summary: string }>();
  for (const item of messages) {
    if (item.role !== "tool" || typeof item.content !== "string") continue;
    try {
      const payload = JSON.parse(item.content) as Record<string, unknown>;
      if (
        typeof payload.name === "string" &&
        typeof payload.download_url === "string"
      )
        found.set(payload.name, {
          name: payload.name,
          summary:
            typeof payload.summary === "string"
              ? payload.summary
              : "Generated file",
        });
    } catch {
      /* Other tool results are plain text. */
    }
  }
  return [...found.values()];
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
    const pending: DesktopSession = {
      id: session?.id || "pending",
      agent_name: agentName,
      title: session?.title || message.slice(0, 80),
      created_at: session?.created_at || "",
      messages: [
        ...(session?.messages || []),
        { role: "user", content: message },
      ],
    };
    setSession(pending);
    try {
      const result = await window.covalentDesktop.sendMessage({
        agent_name: agentName,
        message,
        ...(previous ? { session_id: previous.id } : {}),
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

  return (
    <div
      className={`chat-layout ${conversationsVisible ? "" : "conversations-hidden"} ${traceVisible ? "" : "trace-hidden"}`}
      data-loaded={loaded}
    >
      {conversationsVisible && (
        <section className="surface conversation-list">
          <div className="surface-heading">Recent conversations</div>
          <div className="session-list">
            {sessions.length ? (
              sessions.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  className={`session-item ${session?.id === item.id ? "selected" : ""}`}
                  onClick={() => openSession(item.id)}
                >
                  <MessageSquare size={15} />
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.agent_name}</small>
                  </span>
                </button>
              ))
            ) : (
              <div className="list-empty">
                <MessageSquare size={22} />
                <strong>No conversations yet</strong>
                <p>
                  Your conversation will be saved locally after the first
                  message.
                </p>
              </div>
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
            <span className="chat-heading-title">Conversation</span>
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
              }}
            >
              <Plus size={16} />
            </button>
            <select
              className="agent-picker"
              aria-label="Select agent"
              value={agentName}
              onChange={(event) => {
                setAgentName(event.target.value);
                setSession(null);
              }}
              disabled={!agents.length || busy}
            >
              {agents.length ? (
                agents.map((agent) => (
                  <option key={agent.name} value={agent.name}>
                    {agent.name}
                  </option>
                ))
              ) : (
                <option value="">No agents</option>
              )}
            </select>
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
          {session?.messages.length ? (
            session.messages
              .filter(
                (item) => item.role === "user" || item.role === "assistant",
              )
              .map((item, index) => (
                <div
                  className={`chat-message ${item.role}`}
                  key={`${session.id}-${index}`}
                >
                  <span className="message-role">
                    {item.role === "user" ? "You" : agentName}
                  </span>
                  <div>
                    <MessageContent content={item.content} />
                  </div>
                </div>
              ))
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
          {busy && <p className="response-pending">Agent is responding…</p>}
        </div>
        {session && publishedDownloads(session.messages).length > 0 && (
          <div className="published-downloads">
            {publishedDownloads(session.messages).map((file) => (
              <button
                key={file.name}
                type="button"
                className="secondary-button"
                onClick={() =>
                  void window.covalentDesktop
                    .saveDownload(session.id, file.name)
                    .catch((cause) => setError(String(cause)))
                }
              >
                Download {file.name}
              </button>
            ))}
          </div>
        )}
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
            {session.input_request.questions.map((question) => (
              <label key={question.header}>
                {question.question}
                {question.options.length ? (
                  <select
                    value={answers[question.header] ?? ""}
                    onChange={(event) =>
                      setAnswers((current) => ({
                        ...current,
                        [question.header]: event.target.value,
                      }))
                    }
                  >
                    <option value="">Choose an answer...</option>
                    {question.options.map((option) => (
                      <option key={option.label} value={option.label}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : (
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
                )}
              </label>
            ))}
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
          <div className="surface-heading">Execution trace</div>
          <div className="list-empty">
            <Activity size={22} />
            <strong>Execution traces are coming soon</strong>
            <p>
              Local chat is available. Streaming events and execution traces are
              in development.
            </p>
          </div>
        </section>
      )}
    </div>
  );
}
