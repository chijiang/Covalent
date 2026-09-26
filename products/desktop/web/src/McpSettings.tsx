import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";

const empty: DesktopMcpServer = {
  name: "",
  transport: "streamable_http",
  url: "",
  args: [],
  enabled: true,
};

export function McpSettings() {
  const [items, setItems] = useState<DesktopMcpServer[]>([]);
  const [form, setForm] = useState<DesktopMcpServer>({ ...empty });
  const [isNew, setIsNew] = useState(true);
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [tools, setTools] = useState<DesktopMcpTool[]>([]);
  const [importText, setImportText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function refresh(name?: string) {
    const result = await window.covalentDesktop.listMcpServices();
    setItems(result.items);
    const selected =
      result.items.find((item) => item.name === name) ?? result.items[0];
    if (selected) select(selected);
    else {
      setForm({ ...empty });
      setIsNew(true);
    }
  }
  function select(item: DesktopMcpServer) {
    setForm(item);
    setArgs((item.args ?? []).join("\n"));
    setEnv("");
    setTools([]);
    setIsNew(false);
    setError("");
    setMessage("");
  }
  useEffect(() => {
    void refresh().catch((cause) => setError(String(cause)));
  }, []);
  async function save() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const envValue: Record<string, string> = {};
      for (const line of env
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean)) {
        const separator = line.indexOf("=");
        if (separator < 1)
          throw new Error(
            "Environment variables must use KEY=value, one per line",
          );
        envValue[line.slice(0, separator)] = line.slice(separator + 1);
      }
      const result = await window.covalentDesktop.saveMcpService({
        ...form,
        args: args
          .split("\n")
          .map((value) => value.trim())
          .filter(Boolean),
        env: Object.keys(envValue).length ? envValue : undefined,
      });
      await refresh(result.name);
      setMessage("MCP service saved locally");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  async function inspect() {
    setBusy(true);
    setError("");
    try {
      setTools(
        (await window.covalentDesktop.inspectMcpService(form.name)).items,
      );
      setMessage("Connection succeeded");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    setBusy(true);
    setError("");
    try {
      await window.covalentDesktop.deleteMcpService(form.name);
      await refresh();
      setMessage("MCP service removed");
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  async function importConfig() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const parsed: unknown = JSON.parse(importText);
      if (!parsed || typeof parsed !== "object")
        throw new Error("Expected an MCP configuration object");
      const root = parsed as Record<string, unknown>;
      const servers = root.mcpServers ?? root.mcp_servers ?? root;
      if (!servers || typeof servers !== "object" || Array.isArray(servers))
        throw new Error("Expected mcpServers object");
      const names: string[] = [];
      for (const [name, raw] of Object.entries(servers)) {
        if (!raw || typeof raw !== "object" || Array.isArray(raw))
          throw new Error(`Invalid MCP service: ${name}`);
        const item = raw as Record<string, unknown>;
        const nested =
          item.transport && typeof item.transport === "object"
            ? (item.transport as Record<string, unknown>)
            : {};
        const type =
          typeof item.transport === "string" ? item.transport : nested.type;
        const transport = (type ??
          ((item.command ?? nested.command)
            ? "stdio"
            : "streamable_http")) as DesktopMcpServer["transport"];
        if (!["stdio", "sse", "streamable_http"].includes(transport))
          throw new Error(`Unsupported transport: ${transport}`);
        const env =
          (item.env ?? nested.env) &&
          typeof (item.env ?? nested.env) === "object" &&
          !Array.isArray(item.env ?? nested.env)
            ? ((item.env ?? nested.env) as Record<string, string>)
            : undefined;
        await window.covalentDesktop.saveMcpService({
          name,
          transport,
          command:
            typeof (item.command ?? nested.command) === "string"
              ? String(item.command ?? nested.command)
              : undefined,
          args: Array.isArray(item.args ?? nested.args)
            ? ((item.args ?? nested.args) as string[])
            : [],
          url: String(
            item.url ?? item.endpoint ?? nested.url ?? nested.endpoint ?? "",
          ),
          enabled: true,
          env,
        });
        names.push(name);
      }
      await refresh(names[0]);
      setImportText("");
      setMessage(
        `Imported ${names.length} MCP service${names.length === 1 ? "" : "s"}`,
      );
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="management-layout resource-management-layout">
      <section className="surface inventory-panel">
        <div className="surface-heading">
          MCP services{" "}
          <button
            className="icon-button"
            type="button"
            aria-label="Create MCP service"
            onClick={() => {
              setForm({ ...empty });
              setArgs("");
              setEnv("");
              setTools([]);
              setIsNew(true);
              setError("");
            }}
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="inventory-list">
          {items.map((item) => (
            <button
              key={item.name}
              type="button"
              className={`inventory-item ${!isNew && form.name === item.name ? "selected" : ""}`}
              onClick={() => select(item)}
            >
              <strong>{item.name}</strong>
              <small>
                {item.enabled ? "Active" : "Inactive"} · {item.transport}
              </small>
            </button>
          ))}
          {!items.length && (
            <div className="inventory-empty">No MCP services yet.</div>
          )}
        </div>
      </section>
      <section className="surface detail-panel">
        <div className="surface-heading">
          {isNew ? "Add MCP service" : form.name}
        </div>
        <div className="agent-form resource-form">
          <p className="resource-intro">
            Connect an MCP server for agent tools.
          </p>
          <section className="config-section">
            <div className="form-grid">
              <label>
                Name
                <input
                  value={form.name}
                  disabled={!isNew}
                  onChange={(event) =>
                    setForm({ ...form, name: event.target.value })
                  }
                  placeholder="my-server"
                />
              </label>
              <label>
                Transport
                <select
                  value={form.transport}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      transport: event.target
                        .value as DesktopMcpServer["transport"],
                    })
                  }
                >
                  <option value="streamable_http">Streamable HTTP</option>
                  <option value="sse">SSE</option>
                  <option value="stdio">Stdio</option>
                </select>
              </label>
              {form.transport === "stdio" ? (
                <>
                  <label className="full-width">
                    Command
                    <input
                      value={form.command ?? ""}
                      onChange={(event) =>
                        setForm({ ...form, command: event.target.value })
                      }
                      placeholder="npx"
                    />
                  </label>
                  <label className="full-width">
                    Arguments, one per line
                    <textarea
                      rows={3}
                      value={args}
                      onChange={(event) => setArgs(event.target.value)}
                    />
                  </label>
                </>
              ) : (
                <label className="full-width">
                  URL
                  <input
                    value={form.url ?? ""}
                    onChange={(event) =>
                      setForm({ ...form, url: event.target.value })
                    }
                    placeholder="https://example.com/mcp"
                  />
                </label>
              )}
              <label>
                Runtime
                <select
                  value={form.enabled ? "active" : "inactive"}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      enabled: event.target.value === "active",
                    })
                  }
                >
                  <option value="active">Active</option>
                  <option value="inactive">Inactive</option>
                </select>
              </label>
              <label className="full-width">
                Environment / headers
                <textarea
                  className="resource-short-textarea"
                  rows={3}
                  value={env}
                  onChange={(event) => setEnv(event.target.value)}
                  placeholder="TOKEN=value"
                />
                <small>
                  {form.has_env
                    ? "Values saved securely. Enter new values to replace them."
                    : "Stored securely. Used as headers for HTTP connections."}
                </small>
              </label>
            </div>
            {!isNew && form.has_env && (
              <button
                type="button"
                className="secondary-button"
                disabled={busy}
                onClick={() =>
                  void (async () => {
                    setBusy(true);
                    setError("");
                    try {
                      await window.covalentDesktop.clearMcpEnv(form.name);
                      await refresh(form.name);
                      setMessage("MCP environment cleared");
                    } catch (cause) {
                      setError(String(cause));
                    } finally {
                      setBusy(false);
                    }
                  })()
                }
              >
                Clear saved environment
              </button>
            )}
          </section>
          {tools.length > 0 && (
            <section className="config-section">
              <h3>Available tools</h3>
              <div className="inventory-list">
                {tools.map((tool) => (
                  <div
                    className="inventory-item"
                    key={`${tool.server_name}:${tool.tool_name}`}
                  >
                    <strong>{tool.tool_name}</strong>
                    <small>{tool.description}</small>
                  </div>
                ))}
              </div>
            </section>
          )}
          <details className="config-section resource-import">
            <summary>Import JSON configuration</summary>
            <div className="form-grid">
              <label className="full-width">
                MCP JSON
                <textarea
                  rows={4}
                  value={importText}
                  onChange={(event) => setImportText(event.target.value)}
                  placeholder={'{"mcpServers":{"my-server":{"command":"npx"}}}'}
                />
              </label>
            </div>
            <div className="resource-import-actions">
              <button
                className="secondary-button"
                type="button"
                disabled={busy || !importText.trim()}
                onClick={() => void importConfig()}
              >
                Import JSON
              </button>
            </div>
          </details>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {message && (
            <p className="form-success" role="status">
              {message}
            </p>
          )}
          <div className="form-actions">
            <button
              className="primary-button"
              type="button"
              disabled={busy || !form.name}
              onClick={() => void save()}
            >
              Save service
            </button>
            {!isNew && (
              <>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={busy}
                  onClick={() => void inspect()}
                >
                  Inspect tools
                </button>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Delete MCP service"
                  disabled={busy}
                  onClick={() => void remove()}
                >
                  <Trash2 size={16} />
                </button>
              </>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
