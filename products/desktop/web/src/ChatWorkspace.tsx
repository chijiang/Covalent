import { useEffect, useState } from "react";
import { Activity, MessageSquare, Plus, Send } from "lucide-react";

function contentText(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content);
}

export function ChatWorkspace({ status }: { status: DesktopServiceStatus }) {
  const [agents, setAgents] = useState<DesktopAgent[]>([]);
  const [sessions, setSessions] = useState<DesktopSessionSummary[]>([]);
  const [agentName, setAgentName] = useState("");
  const [session, setSession] = useState<DesktopSession | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");

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

  return (
    <div className="chat-layout" data-loaded={loaded}>
      <section className="surface conversation-list">
        <div className="surface-heading">
          Recent conversations{" "}
          <button
            className="icon-button"
            type="button"
            aria-label="New conversation"
            title="New conversation"
            onClick={() => {
              setSession(null);
              setError("");
            }}
          >
            <Plus size={15} />
          </button>
        </div>
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
                Your conversation will be saved locally after the first message.
              </p>
            </div>
          )}
        </div>
      </section>
      <section className="surface conversation-panel">
        <div className="surface-heading">
          <span>Conversation</span>
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
                  <div>{contentText(item.content)}</div>
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
              busy
            }
            aria-label="Send message"
          >
            <Send size={16} />
          </button>
        </div>
      </section>
      <section className="surface trace-panel">
        <div className="surface-heading">
          Execution trace <span className="subtle-tag">LOCAL</span>
        </div>
        <div className="list-empty">
          <Activity size={22} />
          <strong>Execution traces are coming soon</strong>
          <p>
            Local chat is available. Streaming events and execution traces are
            in development.
          </p>
        </div>
      </section>
    </div>
  );
}
