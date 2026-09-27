import { useEffect, useState } from "react";
import {
  Activity,
  Bot,
  Cable,
  MessageSquare,
  Monitor,
  Moon,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Sun,
} from "lucide-react";
import { ChatWorkspace } from "./ChatWorkspace";
import { AgentWorkspace } from "./AgentWorkspace";
import { ProviderSettings } from "./ProviderSettings";
import { McpSettings } from "./McpSettings";
import { SkillSettings } from "./SkillSettings";
import {
  applyTheme,
  nextTheme,
  saveTheme,
  storedTheme,
  watchSystemTheme,
  type Theme,
} from "./theme";

type Page = "chat" | "agents" | "resources" | "runtime";
const initialStatus: DesktopServiceStatus = {
  phase: "starting",
  protocolVersion: null,
  serviceVersion: null,
  pid: null,
  capabilities: [],
  error: null,
};
const phaseLabels: Record<ServicePhase, string> = {
  stopped: "Stopped",
  starting: "Starting",
  ready: "Running",
  stopping: "Stopping",
  failed: "Failed",
};
const pages: { id: Page; title: string; subtitle: string }[] = [
  {
    id: "chat",
    title: "Chat Workspace",
    subtitle:
      "Chat with agents, inspect execution, and continue conversations.",
  },
  {
    id: "agents",
    title: "Agent settings",
    subtitle: "Create and configure local agents.",
  },
  {
    id: "resources",
    title: "Resources & connections",
    subtitle: "Manage model providers, MCP connections, and skills.",
  },
  {
    id: "runtime",
    title: "Desktop runtime",
    subtitle: "Inspect the local service and host connection.",
  },
];

function StatusBadge({ status }: { status: DesktopServiceStatus }) {
  return (
    <span className={`status-badge ${status.phase}`}>
      <span className={`status-dot ${status.phase}`} />
      {phaseLabels[status.phase]}
    </span>
  );
}

function RuntimeWorkspace({
  status,
  restarting,
  onRestart,
}: {
  status: DesktopServiceStatus;
  restarting: boolean;
  onRestart: () => void;
}) {
  return (
    <div className="runtime-layout">
      <section className="surface runtime-overview">
        <div className="surface-heading">
          Local service
          <StatusBadge status={status} />
        </div>
        <div className="runtime-title">
          <span className="runtime-icon">
            <Activity size={22} />
          </span>
          <div>
            <h2>Python sidecar</h2>
            <p>Local service managed by the Electron host</p>
          </div>
        </div>
        <div className="runtime-facts">
          <div>
            <span>Service version</span>
            <strong>{status.serviceVersion ?? "—"}</strong>
          </div>
          <div>
            <span>Protocol version</span>
            <strong>{status.protocolVersion ?? "—"}</strong>
          </div>
          <div>
            <span>Process ID</span>
            <strong>{status.pid ?? "—"}</strong>
          </div>
          <div>
            <span>Capabilities</span>
            <strong>{status.capabilities.join(", ") || "—"}</strong>
          </div>
        </div>
        {status.error && (
          <p className="error-message" role="alert">
            {status.error}
          </p>
        )}
        <div className="runtime-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={onRestart}
            disabled={
              restarting ||
              status.phase === "starting" ||
              status.phase === "stopping"
            }
          >
            <RefreshCw size={14} />
            {restarting ? "Restarting…" : "Restart service"}
          </button>
        </div>
      </section>
      <section className="surface runtime-note">
        <div className="surface-heading">Desktop host</div>
        <div className="runtime-note-body">
          <span className="runtime-icon">
            <PanelLeft size={21} />
          </span>
          <h3>Local workspace</h3>
          <p>
            Window startup, authentication, health checks, and service restart
            are connected. Agent creation, model credentials, local chat,
            execution traces, Markdown rendering, message editing and file
            downloads are available. Token streaming and templates are in
            development.
          </p>
        </div>
      </section>
    </div>
  );
}

export function App() {
  const [page, setPage] = useState<Page>("chat");
  const [sidebarVisible, setSidebarVisible] = useState(true);
  const [resourceTab, setResourceTab] = useState<
    "providers" | "mcp" | "skills"
  >("providers");
  const [status, setStatus] = useState(initialStatus);
  const [restarting, setRestarting] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => storedTheme());
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);
  useEffect(() => watchSystemTheme(theme), [theme]);
  const next = nextTheme(theme);
  const themeLabels: Record<Theme, string> = {
    light: "Light",
    dark: "Dark",
    system: "System",
  };
  function cycleTheme() {
    saveTheme(next);
    setTheme(next);
  }
  useEffect(() => {
    let mounted = true;
    window.covalentDesktop
      .getServiceStatus()
      .then((value) => mounted && setStatus(value))
      .catch(
        (error) =>
          mounted &&
          setStatus({
            ...initialStatus,
            phase: "failed",
            error: String(error),
          }),
      );
    const unsubscribe = window.covalentDesktop.onServiceStatus(
      (value) => mounted && setStatus(value),
    );
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, []);
  async function restartService() {
    setRestarting(true);
    try {
      setStatus(await window.covalentDesktop.restartService());
    } catch (error) {
      setStatus({ ...initialStatus, phase: "failed", error: String(error) });
    } finally {
      setRestarting(false);
    }
  }
  const meta = pages.find((item) => item.id === page)!;
  return (
    <div className={`desktop-shell ${sidebarVisible ? "" : "sidebar-hidden"}`}>
      <aside className="app-sidebar" aria-label="Main navigation">
        <div className="sidebar-brand">
          <img
            className="sidebar-logo-full"
            src="./logos/covalent-logo-horizontal.png"
            alt="Covalent"
          />
          <img
            className="sidebar-logo-mark"
            src="./logos/covalent-mark.png"
            alt=""
          />
          <button
            className="icon-button sidebar-toggle"
            type="button"
            aria-label={sidebarVisible ? "Collapse sidebar" : "Expand sidebar"}
            title={sidebarVisible ? "Collapse sidebar" : "Expand sidebar"}
            aria-expanded={sidebarVisible}
            onClick={() => setSidebarVisible((visible) => !visible)}
          >
            {sidebarVisible ? (
              <PanelLeftClose size={17} />
            ) : (
              <PanelLeftOpen size={17} />
            )}
          </button>
        </div>
        <div className="sidebar-content">
          <div className="sidebar-group">
            <div className="sidebar-label">WORKSPACE</div>
            <nav aria-label="Workspace">
              <button
                type="button"
                className={`nav-item ${page === "chat" ? "active" : ""}`}
                title="Chat"
                aria-label="Chat"
                onClick={() => setPage("chat")}
              >
                <MessageSquare size={17} />
                <span className="nav-text">Chat</span>
              </button>
            </nav>
          </div>
          <div className="sidebar-group">
            <div className="sidebar-label">SERVICE CONSOLE</div>
            <nav aria-label="Service Console">
              {pages.slice(1, 3).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`nav-item ${page === item.id ? "active" : ""}`}
                  title={item.title}
                  aria-label={item.title}
                  onClick={() => setPage(item.id)}
                >
                  {item.id === "agents" ? (
                    <Bot size={17} />
                  ) : (
                    <Cable size={17} />
                  )}
                  <span className="nav-text">{item.title}</span>
                </button>
              ))}
            </nav>
          </div>
          <div className="sidebar-group">
            <div className="sidebar-label">DESKTOP</div>
            <nav aria-label="Desktop settings">
              <button
                type="button"
                className={`nav-item ${page === "runtime" ? "active" : ""}`}
                title="Desktop runtime"
                aria-label="Desktop runtime"
                onClick={() => setPage("runtime")}
              >
                <Activity size={17} />
                <span className="nav-text">Desktop runtime</span>
              </button>
            </nav>
          </div>
        </div>
        <div className="sidebar-footer">
          <span className="desktop-avatar">
            <img src="./logos/covalent-mark.png" alt="" />
          </span>
          <span className="footer-copy">
            <strong>Local workspace</strong>
            <small>
              <span className={`status-dot ${status.phase}`} />
              {phaseLabels[status.phase]}
            </small>
          </span>
        </div>
      </aside>
      <div className="main-column">
        <header className="app-top-bar">
          <span className="topbar-icon">
            {page === "chat" ? (
              <MessageSquare size={17} />
            ) : page === "agents" ? (
              <Bot size={17} />
            ) : page === "resources" ? (
              <Cable size={17} />
            ) : (
              <Activity size={17} />
            )}
          </span>
          <h1>{meta.title}</h1>
          <p>{meta.subtitle}</p>
          <button
            className="icon-button theme-toggle"
            type="button"
            aria-label={`Theme: ${themeLabels[theme]} (switch to ${themeLabels[next]})`}
            title={`Theme: ${themeLabels[theme]} (switch to ${themeLabels[next]})`}
            onClick={cycleTheme}
          >
            {theme === "light" ? (
              <Sun size={16} />
            ) : theme === "dark" ? (
              <Moon size={16} />
            ) : (
              <Monitor size={16} />
            )}
          </button>
        </header>
        <main
          className={`app-content ${page === "chat" ? "app-content-chat" : ""}`}
        >
          {page === "chat" ? (
            <ChatWorkspace status={status} />
          ) : page === "agents" ? (
            <AgentWorkspace status={status} />
          ) : page === "resources" ? (
            <div className="resource-workspace">
              <div
                className="resource-tabs"
                role="tablist"
                aria-label="Resource settings"
              >
                <button
                  type="button"
                  className={resourceTab === "providers" ? "active" : ""}
                  onClick={() => setResourceTab("providers")}
                >
                  Providers
                </button>
                <button
                  type="button"
                  className={resourceTab === "mcp" ? "active" : ""}
                  onClick={() => setResourceTab("mcp")}
                >
                  MCP services
                </button>
                <button
                  type="button"
                  className={resourceTab === "skills" ? "active" : ""}
                  onClick={() => setResourceTab("skills")}
                >
                  Skills
                </button>
              </div>
              {resourceTab === "providers" && <ProviderSettings />}
              {resourceTab === "mcp" && <McpSettings />}
              {resourceTab === "skills" && <SkillSettings />}
            </div>
          ) : (
            <RuntimeWorkspace
              status={status}
              restarting={restarting}
              onRestart={restartService}
            />
          )}
        </main>
      </div>
    </div>
  );
}
