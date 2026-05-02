"use client";

import { useEffect, useMemo, useState } from "react";

import { ManagementRail } from "@/components/management-rail";
import { callMcpTool, getConfig, inspectMcpServer, saveConfig } from "@/lib/client-api";
import type { ConfigDocument, McpInspectResponse, McpServerConfig } from "@/lib/types";

type EnvFormEntry = {
  id: string;
  key: string;
  value: string;
};

type ServiceFormState = {
  name: string;
  transport: McpServerConfig["transport"];
  endpoint: string;
  command: string;
  argsText: string;
  envEntries: EnvFormEntry[];
};

function createEnvEntry(key = "", value = ""): EnvFormEntry {
  return {
    id: `env-${Math.random().toString(36).slice(2, 10)}`,
    key,
    value,
  };
}

function envValueToString(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (value == null) {
    return "";
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function toEnvEntries(env: Record<string, string> | undefined): EnvFormEntry[] {
  return Object.entries(env || {}).map(([key, value]) => createEnvEntry(key, value));
}

function toServiceForm(server: McpServerConfig | null): ServiceFormState {
  return {
    name: server?.name || "",
    transport: server?.transport || "streamable_http",
    endpoint: server?.url || "",
    command: server?.command || "",
    argsText: server?.args?.join(" ") || "",
    envEntries: toEnvEntries(server?.env),
  };
}

function detectImportedTransport(source: Record<string, unknown>): McpServerConfig["transport"] {
  const transportObject =
    source.transport && !Array.isArray(source.transport) && typeof source.transport === "object"
      ? (source.transport as Record<string, unknown>)
      : null;

  const rawTransport =
    typeof source.transport === "string"
      ? source.transport
      : transportObject && typeof transportObject.type === "string"
        ? transportObject.type
      : typeof source.type === "string"
        ? source.type
        : "";

  switch (rawTransport) {
    case "stdio":
    case "command":
      return "stdio";
    case "sse":
      return "sse";
    case "streamable_http":
    case "http":
      return "streamable_http";
    default:
      if (typeof source.command === "string" && source.command.trim()) {
        return "stdio";
      }
      if (typeof source.url === "string" && source.url.trim()) {
        return "streamable_http";
      }
      if (typeof source.endpoint === "string" && source.endpoint.trim()) {
        return "streamable_http";
      }
      if (transportObject && typeof transportObject.url === "string" && transportObject.url.trim()) {
        return "streamable_http";
      }
      if (transportObject && typeof transportObject.endpoint === "string" && transportObject.endpoint.trim()) {
        return "streamable_http";
      }
      return "streamable_http";
  }
}

function readImportedEnv(source: unknown): Record<string, string> {
  if (source == null) {
    return {};
  }
  if (!source || Array.isArray(source) || typeof source !== "object") {
    throw new Error("Imported environment variables must be an object.");
  }
  return Object.fromEntries(
    Object.entries(source).map(([key, value]) => [key, envValueToString(value)]),
  );
}

function normalizeImportedServer(name: string, source: unknown): McpServerConfig {
  if (!source || Array.isArray(source) || typeof source !== "object") {
    throw new Error(`Service '${name}' must be an object.`);
  }

  const record = source as Record<string, unknown>;
  const transportObject =
    record.transport && !Array.isArray(record.transport) && typeof record.transport === "object"
      ? (record.transport as Record<string, unknown>)
      : null;
  const transport = detectImportedTransport(record);
  const command = typeof record.command === "string" ? record.command.trim() : "";
  const endpoint =
    typeof record.url === "string"
      ? record.url.trim()
      : typeof record.endpoint === "string"
        ? record.endpoint.trim()
        : transportObject && typeof transportObject.url === "string"
          ? transportObject.url.trim()
          : transportObject && typeof transportObject.endpoint === "string"
            ? transportObject.endpoint.trim()
        : "";
  const args = Array.isArray(record.args)
    ? record.args.map((item) => String(item))
    : typeof record.args === "string"
      ? parseArgs(record.args)
      : [];
  const env = readImportedEnv(record.env);

  if (transport === "stdio") {
    if (!command) {
      throw new Error(`Service '${name}' uses stdio and requires a command.`);
    }
    return {
      name,
      transport,
      command,
      args,
      url: null,
      env,
    };
  }

  if (!endpoint) {
    throw new Error(`Service '${name}' requires a URL or endpoint.`);
  }

  return {
    name,
    transport,
    command: null,
    args: [],
    url: endpoint,
    env,
  };
}

function looksLikeServerMap(record: Record<string, unknown>): boolean {
  const reservedKeys = new Set(["name", "transport", "type", "command", "args", "url", "endpoint", "env", "mcp", "mcpServers"]);
  return (
    Object.keys(record).length > 0 &&
    Object.entries(record).every(
      ([key, value]) => !reservedKeys.has(key) && !!value && typeof value === "object" && !Array.isArray(value),
    )
  );
}

function resolveImportedServerMap(record: Record<string, unknown>): Record<string, unknown> | null {
  if (record.mcpServers && !Array.isArray(record.mcpServers) && typeof record.mcpServers === "object") {
    return record.mcpServers as Record<string, unknown>;
  }

  if (record.mcp && !Array.isArray(record.mcp) && typeof record.mcp === "object") {
    const nestedServers = (record.mcp as Record<string, unknown>).servers;
    if (nestedServers && !Array.isArray(nestedServers) && typeof nestedServers === "object") {
      return nestedServers as Record<string, unknown>;
    }
  }

  if (looksLikeServerMap(record)) {
    return record;
  }

  return null;
}

function parseImportedServers(value: string): McpServerConfig[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Service JSON is invalid.");
  }

  if (Array.isArray(parsed)) {
    if (!parsed.length) {
      throw new Error("Service JSON array is empty.");
    }
    return parsed.map((item, index) => {
      if (!item || Array.isArray(item) || typeof item !== "object") {
        throw new Error(`Service entry ${index + 1} must be an object.`);
      }
      const name = typeof (item as Record<string, unknown>).name === "string" ? (item as Record<string, unknown>).name?.trim() : "";
      if (!name) {
        throw new Error(`Service entry ${index + 1} is missing a name.`);
      }
      return normalizeImportedServer(name, item);
    });
  }

  if (!parsed || typeof parsed !== "object") {
    throw new Error("Service JSON must be an object or array.");
  }

  const record = parsed as Record<string, unknown>;
  const serverMap = resolveImportedServerMap(record);
  if (serverMap) {
    const entries = Object.entries(serverMap);
    if (!entries.length) {
      throw new Error("No MCP services were found in the provided JSON.");
    }
    return entries.map(([name, source]) => normalizeImportedServer(name, source));
  }

  if (typeof record.name === "string" && record.name.trim()) {
    return [normalizeImportedServer(record.name.trim(), record)];
  }

  throw new Error("Service JSON must be a service object, an array of services, or a config with mcpServers / mcp.servers.");
}

function downloadTextFile(filename: string, content: string) {
  const blob = new Blob([content], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function transportLabel(transport: McpServerConfig["transport"]): string {
  switch (transport) {
    case "stdio":
      return "stdio";
    case "sse":
      return "sse";
    default:
      return "streamable_http";
  }
}

function targetValue(server: McpServerConfig): string {
  return server.transport === "stdio" ? server.command || "No command configured" : server.url || "No endpoint configured";
}

function targetLabel(server: McpServerConfig): string {
  return server.transport === "stdio" ? "Process" : "Endpoint";
}

function accessLabel(server: McpServerConfig): string {
  return Object.keys(server.env || {}).length > 0 ? "Auth env" : "No auth";
}

function transportCopy(server: McpServerConfig): string {
  if (server.transport === "stdio") {
    return "Runs a local MCP server process through stdio. Save a command and optional args, then test the connection to inspect tools.";
  }
  if (server.transport === "sse") {
    return "Connects to a remote SSE endpoint. Save the URL, then test the connection to inspect tools and auth requirements.";
  }
  return "Connects to a remote streamable HTTP endpoint. Save the URL, then test the connection to inspect tools and auth requirements.";
}

function parseArgs(value: string): string[] {
  return value.trim() ? value.trim().split(/\s+/) : [];
}

function formatToolResult(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  return `${JSON.stringify(content, null, 2)}\n`;
}

function envEntriesToRecord(entries: EnvFormEntry[]): Record<string, string> {
  const env: Record<string, string> = {};

  for (const entry of entries) {
    const key = entry.key.trim();
    if (!key && !entry.value.trim()) {
      continue;
    }
    if (!key) {
      throw new Error("Environment variables require a key.");
    }
    if (Object.prototype.hasOwnProperty.call(env, key)) {
      throw new Error(`Environment variable '${key}' is duplicated.`);
    }
    env[key] = entry.value;
  }

  return env;
}

export function McpWorkspace() {
  const [configDocument, setConfigDocument] = useState<ConfigDocument | null>(null);
  const [editor, setEditor] = useState("[]\n");
  const [selectedName, setSelectedName] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "remote" | "stdio" | "auth">("all");
  const [inspectionByServer, setInspectionByServer] = useState<Record<string, McpInspectResponse>>({});
  const [selectedToolName, setSelectedToolName] = useState("");
  const [toolArgumentsText, setToolArgumentsText] = useState("{}\n");
  const [toolResultText, setToolResultText] = useState("");
  const [form, setForm] = useState<ServiceFormState>(toServiceForm(null));
  const [isImportModalOpen, setIsImportModalOpen] = useState(false);
  const [importJsonText, setImportJsonText] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const nextDocument = await getConfig("mcp");
      setConfigDocument(nextDocument);
      setEditor(nextDocument.raw);
      const list = Array.isArray(nextDocument.data) ? (nextDocument.data as McpServerConfig[]) : [];
      setInspectionByServer((current) => Object.fromEntries(Object.entries(current).filter(([name]) => list.some((server) => server.name === name))));
      setSelectedName((current) => (current && list.some((server) => server.name === current) ? current : list[0]?.name || ""));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load MCP services.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    document.body.classList.add("mcp-services-body");

    return () => {
      document.body.classList.remove("mcp-services-body");
    };
  }, []);

  useEffect(() => {
    void refresh();
  }, []);

  const draftServers = useMemo(() => {
    try {
      const parsed = JSON.parse(editor) as unknown;
      return Array.isArray(parsed) ? (parsed as McpServerConfig[]) : [];
    } catch {
      return [];
    }
  }, [editor]);

  const selectedServer = useMemo(
    () => draftServers.find((server) => server.name === selectedName) ?? draftServers[0] ?? null,
    [draftServers, selectedName],
  );

  const inspection = useMemo(
    () => (selectedServer ? inspectionByServer[selectedServer.name] ?? null : null),
    [inspectionByServer, selectedServer],
  );

  const selectedTool = useMemo(
    () => inspection?.tools.find((tool) => tool.name === selectedToolName) ?? inspection?.tools[0] ?? null,
    [inspection, selectedToolName],
  );

  useEffect(() => {
    if (selectedName && draftServers.some((server) => server.name === selectedName)) {
      return;
    }
    setSelectedName(draftServers[0]?.name || "");
  }, [draftServers, selectedName]);

  useEffect(() => {
    setForm(toServiceForm(selectedServer));
  }, [selectedServer]);

  useEffect(() => {
    if (!inspection?.tools.length) {
      setSelectedToolName("");
      setToolArgumentsText("{}\n");
      setToolResultText("");
      return;
    }
    setSelectedToolName((current) =>
      inspection.tools.some((tool) => tool.name === current) ? current : inspection.tools[0].name,
    );
  }, [inspection]);

  useEffect(() => {
    setToolArgumentsText("{}\n");
    setToolResultText("");
  }, [selectedToolName]);

  const filteredServers = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return draftServers.filter((server) => {
      const hasAuth = Object.keys(server.env || {}).length > 0;
      const isRemote = server.transport !== "stdio";
      if (statusFilter === "remote" && !isRemote) {
        return false;
      }
      if (statusFilter === "stdio" && server.transport !== "stdio") {
        return false;
      }
      if (statusFilter === "auth" && !hasAuth) {
        return false;
      }
      if (!query) {
        return true;
      }
      return `${server.name} ${server.transport} ${server.command || ""} ${server.url || ""} ${Object.keys(server.env || {}).join(" ")}`
        .toLowerCase()
        .includes(query);
    });
  }, [draftServers, searchQuery, statusFilter]);

  const remoteCount = useMemo(() => draftServers.filter((server) => server.transport !== "stdio").length, [draftServers]);
  const authCount = useMemo(() => draftServers.filter((server) => Object.keys(server.env || {}).length > 0).length, [draftServers]);
  const inspectedCount = useMemo(
    () => Object.keys(inspectionByServer).filter((name) => draftServers.some((server) => server.name === name)).length,
    [draftServers, inspectionByServer],
  );

  async function runAction(action: string, runner: () => Promise<void>) {
    setBusyAction(action);
    setError(null);
    setMessage(null);
    try {
      await runner();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : "Action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  function openImportModal() {
    setIsImportModalOpen(true);
    setImportJsonText("");
    setError(null);
  }

  function closeImportModal() {
    setIsImportModalOpen(false);
    setImportJsonText("");
  }

  function createDraftServiceForManualSetup() {
    const baseName = "new-mcp-service";
    const existingNames = new Set(draftServers.map((server) => server.name));
    let nextName = baseName;
    let index = 2;
    while (existingNames.has(nextName)) {
      nextName = `${baseName}-${index}`;
      index += 1;
    }

    const nextServer: McpServerConfig = {
      name: nextName,
      transport: "streamable_http",
      url: "",
      command: null,
      args: [],
      env: {},
    };

    setEditor(`${JSON.stringify([...draftServers, nextServer], null, 2)}\n`);
    setSelectedName(nextServer.name);
    closeImportModal();
    setMessage("Created a draft service. Fill in fields and click Save service to persist.");
    setError(null);
  }

  function updateEnvEntry(entryId: string, field: "key" | "value", value: string) {
    setForm((current) => ({
      ...current,
      envEntries: current.envEntries.map((entry) => (entry.id === entryId ? { ...entry, [field]: value } : entry)),
    }));
  }

  function appendEnvEntry() {
    setForm((current) => ({
      ...current,
      envEntries: [...current.envEntries, createEnvEntry()],
    }));
  }

  function removeEnvEntry(entryId: string) {
    setForm((current) => ({
      ...current,
      envEntries: current.envEntries.filter((entry) => entry.id !== entryId),
    }));
  }

  function importServicesFromJson() {
    void runAction("import", async () => {
      const importedServers = parseImportedServers(importJsonText.trim());
      const existingNames = new Set(draftServers.map((server) => server.name));
      const importedNames = new Set<string>();

      for (const server of importedServers) {
        if (existingNames.has(server.name)) {
          throw new Error(`Service '${server.name}' already exists. Rename or delete it first.`);
        }
        if (importedNames.has(server.name)) {
          throw new Error(`Service '${server.name}' appears more than once in the imported JSON.`);
        }
        importedNames.add(server.name);
      }

      const nextServers = [...draftServers, ...importedServers];
      setEditor(`${JSON.stringify(nextServers, null, 2)}\n`);
      setSelectedName(importedServers[0]?.name || "");
      closeImportModal();
      setMessage(`Imported ${importedServers.length} draft service${importedServers.length === 1 ? "" : "s"}. Save to persist.`);
    });
  }

  function normalizeForm(): McpServerConfig | null {
    if (!selectedServer) {
      return null;
    }

    const nextName = form.name.trim();
    if (!nextName) {
      throw new Error("Service name is required.");
    }

    if (draftServers.some((server) => server.name === nextName && server.name !== selectedServer.name)) {
      throw new Error(`A service named '${nextName}' already exists.`);
    }

    const env = envEntriesToRecord(form.envEntries);
    const endpoint = form.endpoint.trim();
    const command = form.command.trim();
    const args = parseArgs(form.argsText);

    if (form.transport === "stdio") {
      if (!command) {
        throw new Error("stdio transport requires a command.");
      }

      return {
        ...selectedServer,
        name: nextName,
        transport: form.transport,
        url: null,
        command,
        args,
        env,
      };
    }

    if (!endpoint) {
      throw new Error(`${transportLabel(form.transport)} transport requires an endpoint URL.`);
    }

    return {
      ...selectedServer,
      name: nextName,
      transport: form.transport,
      url: endpoint,
      command: null,
      args: [],
      env,
    };
  }

  async function saveSelectedServer() {
    const nextServer = normalizeForm();
    if (!nextServer || !selectedServer) {
      return;
    }
    const nextServers = draftServers.map((server) => (server.name === selectedServer.name ? nextServer : server));
    const nextRaw = `${JSON.stringify(nextServers, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(nextServer.name);
    await runAction("save", async () => {
      if (selectedServer.name !== nextServer.name) {
        setInspectionByServer((current) => {
          const nextInspection = { ...current };
          const previous = nextInspection[selectedServer.name];
          delete nextInspection[selectedServer.name];
          if (previous) {
            nextInspection[nextServer.name] = {
              ...previous,
              server: { ...previous.server, name: nextServer.name },
            };
          }
          return nextInspection;
        });
      }
      const saved = await saveConfig("mcp", nextRaw);
      setConfigDocument(saved);
      setEditor(saved.raw);
      setMessage(`Saved service: ${nextServer.name}.`);
      await refresh();
    });
  }

  async function deleteSelectedServer() {
    if (!selectedServer) {
      return;
    }
    if (!window.confirm(`Delete MCP service '${selectedServer.name}'?`)) {
      return;
    }

    const remainingServers = draftServers.filter((server) => server.name !== selectedServer.name);
    const nextRaw = `${JSON.stringify(remainingServers, null, 2)}\n`;
    setEditor(nextRaw);
    setSelectedName(remainingServers[0]?.name || "");
    setInspectionByServer((current) => {
      const nextInspection = { ...current };
      delete nextInspection[selectedServer.name];
      return nextInspection;
    });
    await runAction("delete", async () => {
      const saved = await saveConfig("mcp", nextRaw);
      setConfigDocument(saved);
      setEditor(saved.raw);
      setMessage(`Deleted service: ${selectedServer.name}.`);
      await refresh();
    });
  }

  async function inspectSelectedServer() {
    const nextServer = normalizeForm();
    if (!nextServer) {
      return;
    }
    await runAction("inspect", async () => {
      const result = await inspectMcpServer(nextServer);
      setInspectionByServer((current) => ({
        ...current,
        [nextServer.name]: result,
      }));
      setSelectedToolName(result.tools[0]?.name || "");
      setMessage(`Discovered ${result.tools.length} tools from ${result.server.name}.`);
    });
  }

  async function runSelectedTool() {
    const nextServer = normalizeForm();
    if (!nextServer || !selectedTool) {
      return;
    }

    let argumentsPayload: unknown = {};
    try {
      argumentsPayload = JSON.parse(toolArgumentsText.trim() || "{}");
    } catch {
      throw new Error("Tool arguments JSON is invalid.");
    }

    if (!argumentsPayload || Array.isArray(argumentsPayload) || typeof argumentsPayload !== "object") {
      throw new Error("Tool arguments must be a JSON object.");
    }

    await runAction("call-tool", async () => {
      const result = await callMcpTool(nextServer, selectedTool.name, argumentsPayload as Record<string, unknown>);
      setToolResultText(formatToolResult(result.content));
      setMessage(`Ran tool: ${selectedTool.name}.`);
    });
  }

  return (
    <main className="workspace-shell console-page-shell mcp-services-shell">
      <section
        className="page-section stack-gap-md mcp-services-page"
        style={{
          display: "flex",
          flex: "1 1 auto",
          flexDirection: "column",
          minHeight: 0,
          height: "100%",
          gap: 16,
        }}
      >
        <div className="page-heading-row">
          <div className="stack-gap-xs">
            <h1 className="page-title is-console-title">MCP services</h1>
            <p className="page-subtitle">Register, inspect, and maintain MCP servers with the same list-and-detail workflow used in skill settings.</p>
          </div>
          <div className="page-action-row">
            <button className="primary-action" onClick={openImportModal} type="button">
              Add service
            </button>
            <button className="secondary-action" onClick={() => downloadTextFile("mcp.json", editor)} type="button">
              Export JSON
            </button>
          </div>
        </div>

        {message ? <p className="inline-feedback">{message}</p> : null}
        {error ? <p className="inline-error">{error}</p> : null}

        <section className="management-layout mcp-services-layout">
          <ManagementRail />

          <div className="management-main mcp-services-main">
            <section className="mcp-services-grid">
              <section className="panel-surface mcp-inventory-panel stack-gap-sm">
                <div className="panel-title-row align-start-row">
                  <div className="stack-gap-2xs grow-block">
                    <h2 className="panel-title">Registered services</h2>
                    <p className="entity-meta">
                      {loading ? "Loading MCP inventory..." : `${filteredServers.length} shown · ${draftServers.length} total · ${remoteCount} remote · ${authCount} auth`}
                    </p>
                  </div>
                  <span className="trace-pill">{inspectedCount} tested</span>
                </div>

                <div className="console-toolbar mcp-toolbar">
                  <label className="search-field grow-block">
                    <input onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search MCP services" value={searchQuery} />
                  </label>
                  <div className="filter-chip-row">
                    {([
                      ["all", "All"],
                      ["remote", "Remote"],
                      ["stdio", "stdio"],
                      ["auth", "Auth"],
                    ] as const).map(([value, label]) => (
                      <button
                        className={statusFilter === value ? "filter-chip is-active" : "filter-chip"}
                        key={value}
                        onClick={() => setStatusFilter(value)}
                        type="button"
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="mcp-list">
                  {loading ? <p className="empty-copy padded-empty">Loading MCP services...</p> : null}
                  {!loading && filteredServers.length === 0 ? <p className="empty-copy padded-empty">No services match the current filter.</p> : null}
                  {!loading
                    ? filteredServers.map((server) => {
                        const inspectedTools = inspectionByServer[server.name]?.tools.length || 0;
                        return (
                      <button
                        className={server.name === selectedName ? "skill-list-item is-active" : "skill-list-item"}
                        key={server.name}
                        onClick={() => setSelectedName(server.name)}
                        type="button"
                        title={targetValue(server)}
                      >
                        <div className="skill-list-title-row">
                          <strong>{server.name}</strong>
                          <span className="skill-meta-pill">{transportLabel(server.transport)}</span>
                        </div>
                        <p className="skill-list-description">{targetValue(server)}</p>
                        <div className="skill-list-meta">
                          <span className="skill-meta-pill">{accessLabel(server)}</span>
                          <span className="skill-meta-pill">{server.transport === "stdio" ? `${server.args?.length || 0} args` : "Remote target"}</span>
                          <span className="skill-meta-pill">{inspectedTools ? `${inspectedTools} tools` : "Not tested"}</span>
                        </div>
                      </button>
                    );
                      })
                    : null}
                </div>
              </section>

              <section className="panel-surface skill-detail-panel mcp-detail-panel">
                {selectedServer ? (
                  <div
                    className="mcp-detail-scroll stack-gap-sm"
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      minHeight: 0,
                      height: "100%",
                      overflow: "auto",
                      gap: 6,
                      paddingRight: 4,
                    }}
                  >
                    <div className="skill-detail-header">
                      <div className="stack-gap-xs grow-block">
                        <div className="skill-detail-title-row">
                          <h2 className="panel-title">{selectedServer.name}</h2>
                          <span className="skill-meta-pill">{transportLabel(selectedServer.transport)}</span>
                        </div>
                        <p className="entity-meta skill-detail-description">{targetValue(selectedServer)}</p>
                        <p className="skill-inline-copy">{transportCopy(selectedServer)}</p>
                      </div>

                      <div className="page-action-row skill-detail-actions">
                        <button className="secondary-action" disabled={busyAction === "inspect"} onClick={() => void inspectSelectedServer()} type="button">
                          {busyAction === "inspect" ? "Testing" : "Test connection"}
                        </button>
                        <button className="primary-action" disabled={busyAction === "save" || !selectedServer} onClick={() => void saveSelectedServer()} type="button">
                          {busyAction === "save" ? "Saving" : "Save service"}
                        </button>
                        <button className="danger-action" disabled={busyAction === "delete"} onClick={() => void deleteSelectedServer()} type="button">
                          {busyAction === "delete" ? "Deleting" : "Delete service"}
                        </button>
                      </div>
                    </div>

                    <div className="skill-meta-rail" role="list" aria-label="MCP service metadata">
                      <div className="skill-meta-chip" role="listitem" aria-label={`Transport ${transportLabel(selectedServer.transport)}`} title={`Transport ${transportLabel(selectedServer.transport)}`}>
                        <strong className="skill-meta-summary">{transportLabel(selectedServer.transport)}</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-path" role="listitem" aria-label={`${targetLabel(selectedServer)} ${targetValue(selectedServer)}`} title={`${targetLabel(selectedServer)} ${targetValue(selectedServer)}`}>
                        <strong className="skill-meta-summary skill-path-value">{targetValue(selectedServer)}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Access ${accessLabel(selectedServer)}`} title={`Access ${accessLabel(selectedServer)}`}>
                        <strong className="skill-meta-summary">{accessLabel(selectedServer)}</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Arguments ${(selectedServer.args || []).length}`} title={`Arguments ${(selectedServer.args || []).length}`}>
                        <strong className="skill-meta-summary">{(selectedServer.args || []).length} args</strong>
                      </div>
                      <div className="skill-meta-chip" role="listitem" aria-label={`Environment variables ${Object.keys(selectedServer.env || {}).length}`} title={`Environment variables ${Object.keys(selectedServer.env || {}).length}`}>
                        <strong className="skill-meta-summary">{Object.keys(selectedServer.env || {}).length} env</strong>
                      </div>
                      <div className="skill-meta-chip skill-meta-chip-resources" role="listitem" aria-label={`Discovered tools ${inspection?.tools.length || 0}`} title={`Discovered tools ${inspection?.tools.length || 0}`}>
                        <strong className="skill-meta-summary skill-meta-inline-value">{inspection?.tools.length || 0} tools</strong>
                      </div>
                    </div>

                    <div className="mcp-form-grid two-up">
                      <section className="form-section stack-gap-sm">
                        <h3 className="editor-section-title">Basics</h3>
                        <label className="form-field">
                          <span>Name</span>
                          <input onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} value={form.name} />
                        </label>
                        <label className="form-field">
                          <span>Transport</span>
                          <select onChange={(event) => setForm((current) => ({ ...current, transport: event.target.value as McpServerConfig["transport"] }))} value={form.transport}>
                            <option value="streamable_http">streamable_http</option>
                            <option value="sse">sse</option>
                            <option value="stdio">stdio</option>
                          </select>
                        </label>
                      </section>

                      <section className="form-section stack-gap-sm">
                        <h3 className="editor-section-title">Connection target</h3>
                        <p className="skill-inline-copy">
                          Remote transports use Endpoint. stdio uses Command plus optional Arguments.
                        </p>
                        <label className="form-field">
                          <span>Endpoint</span>
                          <input
                            disabled={form.transport === "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, endpoint: event.target.value }))}
                            placeholder="https://example.com/mcp/http"
                            value={form.endpoint}
                          />
                        </label>
                        <label className="form-field">
                          <span>Command</span>
                          <input
                            disabled={form.transport !== "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, command: event.target.value }))}
                            placeholder="npx"
                            value={form.command}
                          />
                        </label>
                        <label className="form-field">
                          <span>Arguments</span>
                          <input
                            disabled={form.transport !== "stdio"}
                            onChange={(event) => setForm((current) => ({ ...current, argsText: event.target.value }))}
                            placeholder="-y @modelcontextprotocol/server-filesystem ."
                            value={form.argsText}
                          />
                        </label>
                      </section>
                    </div>

                    <section className="form-section stack-gap-sm">
                      <h3 className="editor-section-title">Environment</h3>
                      <p className="skill-inline-copy">
                        Edit auth and runtime variables as key/value pairs. Values are saved as strings, so JSON payloads should stay stringified when a server expects them.
                      </p>
                      <div className="panel-title-row align-start-row">
                        <div className="stack-gap-2xs grow-block">
                          <p className="entity-meta">Use one row per environment variable.</p>
                        </div>
                        <button className="secondary-action" onClick={appendEnvEntry} type="button">
                          Add variable
                        </button>
                      </div>

                      {form.envEntries.length === 0 ? (
                        <p className="empty-copy padded-empty">No environment variables configured.</p>
                      ) : (
                        <div style={{ display: "grid", gap: 10 }}>
                          {form.envEntries.map((entry) => (
                            <div
                              key={entry.id}
                              style={{
                                display: "grid",
                                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr) auto",
                                gap: 10,
                                alignItems: "end",
                              }}
                            >
                              <label className="form-field">
                                <span>Key</span>
                                <input
                                  onChange={(event) => updateEnvEntry(entry.id, "key", event.target.value)}
                                  placeholder="TAVILY_API_KEY"
                                  value={entry.key}
                                />
                              </label>
                              <label className="form-field">
                                <span>Value</span>
                                <input
                                  onChange={(event) => updateEnvEntry(entry.id, "value", event.target.value)}
                                  placeholder="tvly-..."
                                  value={entry.value}
                                />
                              </label>
                              <button className="danger-action" onClick={() => removeEnvEntry(entry.id)} style={{ alignSelf: "end" }} type="button">
                                Remove
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </section>

                    <section className="detail-block skill-source-shell stack-gap-sm" style={{ minHeight: 320, flex: "0 0 auto" }}>
                      <div className="panel-title-row align-start-row">
                        <div className="stack-gap-2xs grow-block">
                          <h3 className="panel-title">Discovered tools</h3>
                          <p className="entity-meta">Run Test connection after edits to inspect the tool surface exposed by this MCP server.</p>
                        </div>
                        <span className="trace-pill">{inspection?.tools.length || 0} tools</span>
                      </div>

                      {!inspection ? <p className="empty-copy padded-empty">No inspection yet. Test the connection to load tool schemas.</p> : null}
                      {inspection && inspection.tools.length === 0 ? <p className="empty-copy padded-empty">Connection succeeded, but no tools were reported.</p> : null}

                      {inspection && inspection.tools.length > 0 ? (
                        <div className="skill-source-workbench" style={{ minHeight: 280, height: "clamp(280px, 38vh, 420px)" }}>
                          <aside className="skill-file-list">
                            {inspection.tools.map((tool) => (
                              <button
                                className={tool.name === selectedTool?.name ? "skill-file-item is-active" : "skill-file-item"}
                                key={tool.name}
                                onClick={() => setSelectedToolName(tool.name)}
                                type="button"
                              >
                                {tool.name}
                              </button>
                            ))}
                          </aside>

                          <section className="skill-file-preview">
                            <div className="skill-file-preview-head">
                              <strong>{selectedTool?.name || "Tool preview"}</strong>
                              <span>input schema</span>
                            </div>
                            {selectedTool?.description ? <p className="mcp-tool-description">{selectedTool.description}</p> : null}
                            <pre className="code-preview skill-source-preview">{selectedTool ? `${JSON.stringify(selectedTool.input_schema, null, 2)}\n` : ""}</pre>
                          </section>
                        </div>
                      ) : null}
                    </section>

                    {selectedTool ? (
                      <section className="detail-block subtle-block mcp-tool-runner stack-gap-sm">
                        <div className="panel-title-row align-start-row">
                          <div className="stack-gap-2xs grow-block">
                            <h3 className="panel-title">Run selected tool</h3>
                            <p className="entity-meta">Send a JSON object to the selected MCP tool and inspect the raw response.</p>
                          </div>
                          <button className="secondary-action" disabled={busyAction === "call-tool"} onClick={() => void runSelectedTool()} type="button">
                            {busyAction === "call-tool" ? "Running" : `Run ${selectedTool.name}`}
                          </button>
                        </div>

                        <label className="form-field">
                          <span>Arguments JSON</span>
                          <textarea onChange={(event) => setToolArgumentsText(event.target.value)} rows={7} value={toolArgumentsText} />
                        </label>

                        <div className="mcp-tool-result">
                          <div className="skill-file-preview-head">
                            <strong>Tool result</strong>
                            <span>raw output</span>
                          </div>
                          <pre className="code-preview skill-source-preview">{toolResultText || "Run the selected tool to preview its output.\n"}</pre>
                        </div>
                      </section>
                    ) : null}
                  </div>
                ) : (
                  <div className="skill-detail-empty">
                    <h2 className="panel-title">No service selected</h2>
                    <p className="entity-meta">Choose a service from the inventory or add a new MCP server to start editing.</p>
                  </div>
                )}
              </section>
            </section>
          </div>
        </section>

        {isImportModalOpen ? (
          <div className="modal-overlay" onClick={closeImportModal} role="presentation">
            <section className="modal-card is-compact" onClick={(event) => event.stopPropagation()}>
              <div className="panel-title-row align-start-row">
                <div className="stack-gap-2xs grow-block">
                  <h2 className="panel-title">Build Service From JSON</h2>
                  <p className="entity-meta">Paste a single MCP service JSON object, an array of services, or a config containing `mcpServers` or `mcp.servers`.</p>
                </div>
                <button className="secondary-action" onClick={closeImportModal} type="button">
                  Close
                </button>
              </div>

              <div className="new-skill-shell stack-gap-md">
                {error ? <p className="inline-error">{error}</p> : null}

                <label className="form-field">
                  <span>Service JSON</span>
                  <textarea
                    onChange={(event) => setImportJsonText(event.target.value)}
                    placeholder={`{
  "mcpServers": {
    "query-server": {
      "transport": {
        "type": "sse",
        "url": "http://127.0.0.1:8001/sse"
      }
    }
  }
}`}
                    rows={16}
                    value={importJsonText}
                  />
                </label>

                <p className="skill-inline-copy">
                  Import creates draft services in the list immediately. Review anything you want to adjust, then use Save service to persist the config.
                </p>

                <div className="align-end-row page-action-row">
                  <button className="secondary-action" onClick={createDraftServiceForManualSetup} type="button">
                    Manual setup
                  </button>
                  <button className="secondary-action" onClick={closeImportModal} type="button">
                    Cancel
                  </button>
                  <button className="primary-action" disabled={busyAction === "import"} onClick={importServicesFromJson} type="button">
                    {busyAction === "import" ? "Importing" : "Build service"}
                  </button>
                </div>
              </div>
            </section>
          </div>
        ) : null}
      </section>
    </main>
  );
}